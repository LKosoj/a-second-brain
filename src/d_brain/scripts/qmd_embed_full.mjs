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
const _llmMod = await import(pathToFileURL(`${QMD_NODE_PATH}/dist/llm.js`).href);
const {
  chunkDocument,
  chunkDocumentByTokens,
  clearAllEmbeddings,
  createStore,
  extractTitle,
  getEmbeddingFingerprint,
  insertEmbedding,
} = _storeMod;
const {
  disposeDefaultLlamaCpp,
  formatDocForEmbedding,
  getDefaultLlamaCpp,
} = _llmMod;
import {
  embedTextsViaRemote,
  formatRemoteDocument,
  getDesiredEmbedModel,
  shouldUseRemoteEmbeddings,
} from "./qmd_remote_embeddings.mjs";

const BATCH_SIZE = 16;

function parseArgs(argv) {
  return {
    force: argv.includes("--force") || argv.includes("-f"),
  };
}

function log(message) {
  process.stdout.write(`${message}\n`);
}

async function embedTexts(llm, texts) {
  try {
    const batch = await llm.embedBatch(texts);
    if (batch.length !== texts.length) {
      throw new Error(
        `embedBatch length mismatch: expected ${texts.length}, got ${batch.length}`,
      );
    }
    return batch;
  } catch (error) {
    log(
      `[qmd-embed] batch fallback: ${error instanceof Error ? error.message : String(error)}`,
    );
    const results = [];
    for (const text of texts) {
      try {
        results.push(await llm.embed(text));
      } catch {
        results.push(null);
      }
    }
    return results;
  }
}

async function embedTextsRemote(texts, model) {
  try {
    return await embedTextsViaRemote(texts, { model });
  } catch (error) {
    if (texts.length === 1) {
      throw error;
    }
    log(
      `[qmd-embed] remote batch fallback: ${error instanceof Error ? error.message : String(error)}`,
    );
    const results = [];
    for (const text of texts) {
      try {
        const [result] = await embedTextsViaRemote([text], { model });
        results.push(result ?? null);
      } catch {
        results.push(null);
      }
    }
    return results;
  }
}

async function main() {
  const { force } = parseArgs(process.argv.slice(2));
  const dbPath = process.env.QMD_DB_PATH;
  const model = getDesiredEmbedModel() || "unknown";
  const fingerprint = getEmbeddingFingerprint(model);
  const useRemoteEmbeddings = shouldUseRemoteEmbeddings(model);

  if (!dbPath) {
    throw new Error("QMD_DB_PATH is required");
  }

  const store = createStore(dbPath);
  const db = store.db;
  const llm = useRemoteEmbeddings ? null : getDefaultLlamaCpp();

  const docs = db
    .prepare(
      `
        SELECT d.hash, MIN(d.path) AS path, c.doc AS body
        FROM documents d
        JOIN content c ON d.hash = c.hash
        WHERE d.active = 1
        GROUP BY d.hash
        ORDER BY MIN(d.path)
      `,
    )
    .all();

  const existingSeqsStmt = db.prepare(
    "SELECT seq FROM content_vectors WHERE hash = ? ORDER BY seq",
  );
  const deleteHashSeqStmt = db.prepare(
    "DELETE FROM content_vectors WHERE hash = ?",
  );
  const countVectorRowsStmt = db.prepare(
    "SELECT COUNT(*) AS count FROM content_vectors",
  );
  const vectorsTableExistsStmt = db.prepare(
    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'vectors_vec'",
  );

  function deleteHashVectors(hash) {
    if (!vectorsTableExistsStmt.get()) {
      return;
    }
    db.prepare("DELETE FROM vectors_vec WHERE hash_seq GLOB ?").run(`${hash}_*`);
  }

  let totalDocsProcessed = 0;
  let totalDocsCompleted = 0;
  let totalChunksEmbedded = 0;
  let totalChunksSkipped = 0;
  let unresolvedChunks = 0;
  let firstDimension = null;
  const startedAt = Date.now();

  if (force) {
    log("[qmd-embed] force mode: clearing existing embeddings");
    clearAllEmbeddings(db);
  }

  log(
    `[qmd-embed] start docs=${docs.length} model=${model} remote=${useRemoteEmbeddings ? "true" : "false"} force=${force ? "true" : "false"}`,
  );

  try {
    let pass = 0;
    while (true) {
      pass += 1;
      const beforeRows = Number(countVectorRowsStmt.get().count);
      let passDocsProcessed = 0;
      let passDocsCompleted = 0;
      let passChunksEmbedded = 0;
      let passChunksSkipped = 0;
      let passRemainingChunks = 0;

      log(`[qmd-embed] pass=${pass} begin vector_rows=${beforeRows}`);

      for (const doc of docs) {
        passDocsProcessed += 1;
        const body = doc.body || "";
        if (!body.trim()) {
          continue;
        }

        const title = extractTitle(body, doc.path);
        const chunks = useRemoteEmbeddings
          ? chunkDocument(body)
          : await chunkDocumentByTokens(body);
        if (chunks.length === 0) {
          continue;
        }

        const expectedSeqs = new Set(chunks.map((_, index) => index));
        let existingSeqs =
          force && pass === 1
            ? []
            : existingSeqsStmt.all(doc.hash).map((row) => Number(row.seq));

        const extraSeqs = existingSeqs.filter((seq) => !expectedSeqs.has(seq));
        if (extraSeqs.length > 0) {
          deleteHashSeqStmt.run(doc.hash);
          deleteHashVectors(doc.hash);
          existingSeqs = [];
        }

        const missing = chunks
          .map((chunk, seq) => ({ chunk, seq }))
          .filter(({ seq }) => !existingSeqs.includes(seq));

        if (missing.length === 0) {
          passDocsCompleted += 1;
          passChunksSkipped += chunks.length;
          if (passDocsProcessed % 10 === 0 || passDocsProcessed === docs.length) {
            log(
              `[qmd-embed] pass=${pass} progress docs=${passDocsProcessed}/${docs.length} completed=${passDocsCompleted} embedded=${passChunksEmbedded} skipped=${passChunksSkipped} remaining=${passRemainingChunks}`,
            );
          }
          continue;
        }

        const formattedTexts = missing.map(({ chunk }) =>
          useRemoteEmbeddings
            ? formatRemoteDocument(chunk.text, title, model)
            : formatDocForEmbedding(chunk.text, title, model),
        );

        for (let offset = 0; offset < missing.length; offset += BATCH_SIZE) {
          const batch = missing.slice(offset, offset + BATCH_SIZE);
          const texts = formattedTexts.slice(offset, offset + BATCH_SIZE);
          const results = useRemoteEmbeddings
            ? await embedTextsRemote(texts, model)
            : await embedTexts(llm, texts);

          for (let index = 0; index < batch.length; index += 1) {
            const embedding = results[index];
            const { chunk, seq } = batch[index];
            if (!embedding?.embedding?.length) {
              continue;
            }
            if (firstDimension == null) {
              firstDimension = embedding.embedding.length;
              store.ensureVecTable(firstDimension);
            }
            insertEmbedding(
              db,
              doc.hash,
              seq,
              chunk.pos,
              new Float32Array(embedding.embedding),
              model,
              new Date().toISOString(),
              chunks.length,
              fingerprint,
            );
            passChunksEmbedded += 1;
          }
        }

        const remainingSeqs = existingSeqsStmt
          .all(doc.hash)
          .map((row) => Number(row.seq));
        const remaining = chunks
          .map((_, seq) => seq)
          .filter((seq) => !remainingSeqs.includes(seq));
        if (remaining.length === 0) {
          passDocsCompleted += 1;
        } else {
          passRemainingChunks += remaining.length;
          log(
            `[qmd-embed] pass=${pass} partial path=${doc.path} missing=${remaining.length}`,
          );
        }

        if (passDocsProcessed % 5 === 0 || passDocsProcessed === docs.length) {
          log(
            `[qmd-embed] pass=${pass} progress docs=${passDocsProcessed}/${docs.length} completed=${passDocsCompleted} embedded=${passChunksEmbedded} skipped=${passChunksSkipped} remaining=${passRemainingChunks}`,
          );
        }
      }

      const afterRows = Number(countVectorRowsStmt.get().count);
      totalDocsProcessed += passDocsProcessed;
      totalDocsCompleted = Math.max(totalDocsCompleted, passDocsCompleted);
      totalChunksEmbedded += passChunksEmbedded;
      totalChunksSkipped += passChunksSkipped;
      unresolvedChunks = passRemainingChunks;

      log(
        `[qmd-embed] pass=${pass} done vector_rows=${afterRows} delta=${afterRows - beforeRows} remaining=${passRemainingChunks}`,
      );

      if (passRemainingChunks === 0) {
        break;
      }
      if (afterRows <= beforeRows || passChunksEmbedded === 0) {
        log(
          `[qmd-embed] pass=${pass} stopped: no further progress while ${passRemainingChunks} chunks remain`,
        );
        break;
      }
    }
  } finally {
    await disposeDefaultLlamaCpp();
    store.close();
  }

  const durationSeconds = ((Date.now() - startedAt) / 1000).toFixed(1);
  log(
    `[qmd-embed] done docs=${totalDocsProcessed} completed=${totalDocsCompleted}/${docs.length} embedded=${totalChunksEmbedded} skipped=${totalChunksSkipped} unresolved=${unresolvedChunks} duration_s=${durationSeconds}`,
  );

  if (unresolvedChunks > 0) {
    process.exitCode = 1;
  }
}

main().catch(async (error) => {
  try {
    await disposeDefaultLlamaCpp();
  } catch {}
  const message = error instanceof Error ? error.stack || error.message : String(error);
  process.stderr.write(`${message}\n`);
  process.exitCode = 1;
});
