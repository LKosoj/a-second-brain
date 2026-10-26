#!/usr/bin/env node

import { existsSync } from "node:fs";
import { pathToFileURL } from "node:url";

const QMD_NODE_CANDIDATES = [
  `${process.env.HOME}/.local/lib/node_modules/@tobilu/qmd`,
  "/usr/local/lib/node_modules/@tobilu/qmd",
];
const QMD_NODE_PATH =
  QMD_NODE_CANDIDATES.find((p) => existsSync(p)) ?? QMD_NODE_CANDIDATES[0];
const _storeMod = await import(pathToFileURL(`${QMD_NODE_PATH}/dist/store.js`).href);
const { createStore, extractSnippet, reciprocalRankFusion } = _storeMod;
import {
  embedTextsViaRemote,
  formatRemoteQuery,
  getDesiredEmbedModel,
  getDesiredRerankModel,
  rerankDocumentsViaRemote,
} from "./qmd_remote_embeddings.mjs";

const VEC_WEIGHT = 1.0;
const FTS_WEIGHT = 1.15;
const MAX_RRF_BOOST = 0.14;
const MAX_TITLE_BOOST = 0.12;
const HYBRID_SCORE_WEIGHT = 0.65;
const RERANK_SCORE_WEIGHT = 0.35;

function parseArgs(argv) {
  let limit = 12;
  const queryParts = [];
  const scope = [];

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    // Everything after "--" is the query, even if it looks like an option.
    if (arg === "--") {
      queryParts.push(...argv.slice(index + 1));
      break;
    }
    if (arg === "--scope-uri" && index + 1 < argv.length) {
      scope.push(argv[++index]);
      continue;
    }
    if ((arg === "-n" || arg === "--limit") && index + 1 < argv.length) {
      const parsed = Number.parseInt(argv[index + 1], 10);
      if (Number.isInteger(parsed) && parsed > 0) {
        limit = parsed;
      }
      index += 1;
      continue;
    }
    queryParts.push(arg);
  }

  return {
    limit,
    scope,
    query: queryParts.join(" ").trim(),
  };
}

export function searchScoped(store, query, embedding, scope, limit) {
  const uri = "('qmd://' || d.collection || '/' || d.path)";
  const where = `EXISTS (SELECT 1 FROM json_each(?) s
    WHERE ${uri} = s.value OR instr(${uri}, s.value || '/') = 1)`;
  const params = [JSON.stringify(scope.map((path) => path.replace(/\/$/, "")))];
  const documents = store.db.prepare(`
    SELECT d.id, d.hash, d.title, ${uri} AS filepath, c.doc AS body
    FROM documents d JOIN content c ON c.hash = d.hash
    WHERE d.active = 1 AND (${where})
  `).all(...params);
  if (!documents.length) return [[], []];

  const byHash = new Map();
  for (const doc of documents) {
    const entries = byHash.get(doc.hash) || [];
    entries.push(doc);
    byHash.set(doc.hash, entries);
  }
  const vecResults = [];
  const hasVectors = store.db.prepare(
    "SELECT name FROM sqlite_master WHERE name = 'vectors_vec'",
  ).get();
  if (embedding?.length && hasVectors) {
    const chunks = store.db.prepare(`
      SELECT DISTINCT cv.hash || '_' || cv.seq AS hash_seq, cv.hash, cv.pos
      FROM content_vectors cv JOIN documents d ON d.hash = cv.hash
      WHERE d.active = 1 AND (${where})
    `).all(...params);
    if (chunks.length) {
      const chunksByKey = new Map(chunks.map((chunk) => [chunk.hash_seq, chunk]));
      const matches = store.db.prepare(`
        SELECT hash_seq, distance FROM vectors_vec
        WHERE embedding MATCH ? AND k = ?
          AND hash_seq IN (SELECT value FROM json_each(?))
      `).all(
        new Float32Array(embedding),
        Math.min(limit * 3, chunks.length),
        JSON.stringify(chunks.map((chunk) => chunk.hash_seq)),
      );
      const seen = new Set();
      for (const match of matches) {
        const chunk = chunksByKey.get(match.hash_seq);
        for (const doc of byHash.get(chunk.hash) || []) {
          if (seen.has(doc.filepath)) continue;
          seen.add(doc.filepath);
          vecResults.push({
            ...doc, score: 1 - match.distance, source: "vec", chunkPos: chunk.pos,
          });
        }
      }
    }
  }
  const terms = query.match(/[\p{L}\p{N}_-]+/gu) || [];
  const ftsQuery = terms.map((term) => `"${term}"`).join(" OR ");
  const ftsResults = ftsQuery ? store.db.prepare(`
    SELECT d.title, ${uri} AS filepath, c.doc AS body,
      bm25(documents_fts, 1.5, 4.0, 1.0) AS rank
    FROM documents_fts JOIN documents d ON d.id = documents_fts.rowid
    JOIN content c ON c.hash = d.hash
    WHERE documents_fts MATCH ? AND d.active = 1 AND (${where})
    ORDER BY rank LIMIT ?
  `).all(ftsQuery, ...params, limit).map((doc) => ({
    ...doc, score: Math.abs(doc.rank) / (1 + Math.abs(doc.rank)), source: "fts",
  })) : [];
  return [vecResults.slice(0, limit), ftsResults];
}

function tokenizeQuery(query) {
  return [...new Set(
    query
      .toLowerCase()
      .split(/[^\p{L}\p{N}]+/u)
      .filter((token) => token.length >= 2),
  )];
}

function titleMatchBoost(query, title, displayPath) {
  const normalizedQuery = query.trim().toLowerCase();
  const haystack = `${title || ""}\n${displayPath || ""}`.toLowerCase();
  if (!normalizedQuery || !haystack) {
    return 0;
  }

  let boost = 0;
  if (normalizedQuery.length >= 4 && haystack.includes(normalizedQuery)) {
    boost += 0.06;
  }

  const tokens = tokenizeQuery(query);
  if (tokens.length === 0) {
    return boost;
  }

  const matches = tokens.filter((token) => haystack.includes(token)).length;
  if (matches === 0) {
    return boost;
  }
  return Math.min(boost + (matches / tokens.length) * 0.06, MAX_TITLE_BOOST);
}

function toRankedResult(item) {
  return {
    file: item.filepath,
    displayPath: item.displayPath || item.filepath,
    title: item.title || item.displayPath || item.filepath,
    body: item.body || "",
    score: Number(item.score) || 0,
  };
}

function choosePrimaryResult(entry) {
  if (entry.vec) {
    return entry.vec;
  }
  return entry.fts;
}

function buildSnippet(item, query, maxChars = 500) {
  const snippet =
    extractSnippet(
      item.body || "",
      query,
      maxChars,
      Number.isInteger(item.chunkPos) ? item.chunkPos : undefined,
    ).snippet || "";
  return snippet;
}

function buildHybridResults(query, vecResults, ftsResults, limit) {
  const byFile = new Map();

  for (const item of vecResults) {
    const existing = byFile.get(item.filepath) || {};
    byFile.set(item.filepath, { ...existing, vec: item });
  }
  for (const item of ftsResults) {
    const existing = byFile.get(item.filepath) || {};
    byFile.set(item.filepath, { ...existing, fts: item });
  }

  const rankedLists = [];
  const weights = [];
  if (vecResults.length > 0) {
    rankedLists.push(vecResults.map(toRankedResult));
    weights.push(VEC_WEIGHT);
  }
  if (ftsResults.length > 0) {
    rankedLists.push(ftsResults.map(toRankedResult));
    weights.push(FTS_WEIGHT);
  }
  const fused = reciprocalRankFusion(rankedLists, weights);
  const rrfScores = new Map(fused.map((item) => [item.file, Number(item.score) || 0]));

  return Array.from(byFile.values())
    .map((entry) => {
      const primary = choosePrimaryResult(entry);
      const retrievalScore = Math.max(
        Number(entry.vec?.score) || 0,
        Number(entry.fts?.score) || 0,
      );
      const rrfScore = rrfScores.get(primary.filepath) || 0;
      const score = Math.min(
        retrievalScore +
          Math.min(rrfScore * 1.5, MAX_RRF_BOOST) +
          titleMatchBoost(query, primary.title, primary.displayPath),
        1,
      );

      return {
        ...primary,
        score,
        retrievalScore,
        rrfScore,
        source: entry.vec && entry.fts ? "hybrid" : primary.source,
      };
    })
    .sort((left, right) => right.score - left.score)
    .slice(0, limit);
}

function buildRerankDocument(item, query) {
  const title = item.title || item.displayPath || item.filepath;
  const snippet = buildSnippet(item, query, 1400);
  const body = snippet || String(item.body || "").slice(0, 1400);
  return title ? `${title}\n\n${body}` : body;
}

function isLikelyLocalRerankModel(model) {
  return model.startsWith("hf:") || model.includes(":");
}

async function maybeRerankResults(store, query, results) {
  const model = getDesiredRerankModel();
  if (!model || results.length === 0) {
    return results;
  }

  const documents = results.map((item) => ({
    file: item.filepath,
    text: buildRerankDocument(item, query),
  }));
  let reranked = null;

  const tryLocalFirst = isLikelyLocalRerankModel(model);
  if (tryLocalFirst) {
    try {
      reranked = await store.rerank(query, documents, model);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      process.stderr.write(`qmd local rerank skipped: ${message}\n`);
    }
  }

  if (!reranked) {
    try {
      const remote = await rerankDocumentsViaRemote(
        query,
        documents.map((item) => item.text),
        { model },
      );
      reranked = remote
        .map((item, index) =>
          item
            ? {
                file: documents[index].file,
                score: Math.max(0, Math.min(1, Number(item.score) || 0)),
              }
            : null,
        )
        .filter(Boolean);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      process.stderr.write(`qmd remote rerank skipped: ${message}\n`);
    }
  }

  if (!reranked && !tryLocalFirst) {
    try {
      reranked = await store.rerank(query, documents, model);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      process.stderr.write(`qmd local rerank fallback skipped: ${message}\n`);
    }
  }

  if (!reranked) {
    return results;
  }

  const rerankScores = new Map(
    reranked.map((item) => [item.file, Math.max(0, Math.min(1, Number(item.score) || 0))]),
  );

  return results
    .map((item) => {
      const rerankScore = rerankScores.get(item.filepath);
      if (typeof rerankScore !== "number") {
        return item;
      }
      return {
        ...item,
        rerankScore,
        score: Math.max(
          0,
          Math.min(
            1,
            item.score * HYBRID_SCORE_WEIGHT + rerankScore * RERANK_SCORE_WEIGHT,
          ),
        ),
      };
    })
    .sort((left, right) => right.score - left.score);
}

async function main() {
  const { limit, query, scope } = parseArgs(process.argv.slice(2));
  const dbPath = process.env.QMD_DB_PATH;
  const model = getDesiredEmbedModel();

  if (!dbPath) {
    throw new Error("QMD_DB_PATH is required");
  }
  if (!query) {
    throw new Error("query is required");
  }
  if (!model) {
    throw new Error("EMB_MODEL is required for remote qmd query");
  }

  const store = createStore(dbPath);
  let localLlm;
  try {
    let queryEmbedding;
    if (process.env.EMB_MODEL?.trim()) {
      [queryEmbedding] = await embedTextsViaRemote(
        [formatRemoteQuery(query, model)], { model },
      );
    } else {
      const { getDefaultLlamaCpp } = await import(
        pathToFileURL(`${QMD_NODE_PATH}/dist/llm.js`).href
      );
      localLlm = getDefaultLlamaCpp();
      queryEmbedding = await localLlm.embed(
        _storeMod.formatQueryForEmbedding(query, model), { model, isQuery: true },
      );
    }

    if (!queryEmbedding?.embedding?.length) {
      process.stdout.write("[]\n");
      return;
    }

    const [vecResults, ftsResults] = scope.length
      ? searchScoped(store, query, queryEmbedding.embedding, scope, limit)
      : [
        await store.searchVec(
          query, model, limit, undefined, undefined, queryEmbedding.embedding,
        ),
        store.searchFTS(query, limit),
      ];
    const hybridResults = buildHybridResults(query, vecResults, ftsResults, limit);
    const results = await maybeRerankResults(store, query, hybridResults);

    const payload = results.map((item) => ({
      file: item.filepath,
      title: item.title || item.displayPath || item.filepath,
      score: item.score,
      retrievalScore:
        typeof item.retrievalScore === "number" ? item.retrievalScore : item.score,
      rerankScore:
        typeof item.rerankScore === "number" ? item.rerankScore : null,
      source: item.source || "",
      snippet: buildSnippet(item, query),
    }));

    process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
  } finally {
    store.close();
    if (localLlm) await localLlm.dispose();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    const message = error instanceof Error ? error.stack || error.message : String(error);
    process.stderr.write(`${message}\n`);
    process.exitCode = 1;
  });
}
