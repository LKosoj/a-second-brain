#!/usr/bin/env node

import { existsSync } from "node:fs";
import { pathToFileURL } from "node:url";

const QMD_NODE_CANDIDATES = [
  `${process.env.HOME}/.local/lib/node_modules/@tobilu/qmd`,
  "/usr/local/lib/node_modules/@tobilu/qmd",
];
const QMD_NODE_PATH =
  QMD_NODE_CANDIDATES.find((p) => existsSync(p)) ?? QMD_NODE_CANDIDATES[0];
const _llmMod = await import(pathToFileURL(`${QMD_NODE_PATH}/dist/llm.js`).href);
const { isQwen3EmbeddingModel } = _llmMod;

const DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1";

export function getRemoteEmbedModel() {
  return (process.env.EMB_MODEL || "").trim();
}

export function getDesiredEmbedModel() {
  return (getRemoteEmbedModel() || process.env.QMD_EMBED_MODEL || "").trim();
}

export function getDesiredRerankModel() {
  return (
    process.env.QMD_RERANK_MODEL ||
    process.env.RERANK_MODEL ||
    ""
  ).trim();
}

export function shouldUseRemoteEmbeddings() {
  return Boolean(getRemoteEmbedModel());
}

export function formatRemoteQuery(query, model = getDesiredEmbedModel()) {
  if (isQwen3EmbeddingModel(model)) {
    return `Instruct: Retrieve relevant documents for the given query\nQuery: ${query}`;
  }
  return query;
}

export function formatRemoteDocument(text, title, model = getDesiredEmbedModel()) {
  if (isQwen3EmbeddingModel(model)) {
    return title ? `${title}\n${text}` : text;
  }
  return title ? `${title}\n\n${text}` : text;
}

function getBaseUrl() {
  return (
    process.env.OPENAI_BASE_URL ||
    process.env.BASE_URL ||
    DEFAULT_OPENAI_BASE_URL
  ).trim();
}

function getEmbeddingsUrl() {
  const baseUrl = getBaseUrl().replace(/\/+$/, "");
  if (baseUrl.endsWith("/embeddings")) {
    return baseUrl;
  }
  if (baseUrl.endsWith("/v1")) {
    return `${baseUrl}/embeddings`;
  }
  return `${baseUrl}/v1/embeddings`;
}

function getRerankUrl() {
  const baseUrl = getBaseUrl().replace(/\/+$/, "");
  if (baseUrl.endsWith("/rerank")) {
    return baseUrl;
  }
  if (baseUrl.endsWith("/v1")) {
    return `${baseUrl}/rerank`;
  }
  return `${baseUrl}/v1/rerank`;
}

function normalizeEmbeddingPayload(payload, expectedCount) {
  if (!payload || !Array.isArray(payload.data)) {
    throw new Error("invalid embeddings response payload");
  }
  const indexed = new Map();
  payload.data.forEach((item, index) => {
    const key = Number.isInteger(item?.index) ? item.index : index;
    indexed.set(key, item?.embedding);
  });
  return Array.from({ length: expectedCount }, (_, index) => {
    const embedding = indexed.get(index);
    if (!Array.isArray(embedding) || embedding.length === 0) {
      return null;
    }
    return { embedding };
  });
}

function normalizeRerankPayload(payload, expectedCount) {
  const items = Array.isArray(payload?.results)
    ? payload.results
    : Array.isArray(payload?.data)
      ? payload.data
      : [];
  if (!Array.isArray(items)) {
    throw new Error("invalid rerank response payload");
  }
  const indexed = new Map();
  items.forEach((item, index) => {
    const key = Number.isInteger(item?.index) ? item.index : index;
    const score =
      typeof item?.relevance_score === "number"
        ? item.relevance_score
        : typeof item?.score === "number"
          ? item.score
          : typeof item?.relevanceScore === "number"
            ? item.relevanceScore
            : null;
    if (score !== null) {
      indexed.set(key, score);
    }
  });
  return Array.from({ length: expectedCount }, (_, index) => {
    const score = indexed.get(index);
    if (typeof score !== "number") {
      return null;
    }
    return { index, score };
  });
}

export async function embedTextsViaRemote(
  texts,
  { model = getDesiredEmbedModel() } = {},
) {
  const apiKey = (process.env.OPENAI_API_KEY || "").trim();
  if (!apiKey) {
    throw new Error("OPENAI_API_KEY is required for remote embeddings");
  }
  if (!model) {
    throw new Error("EMB_MODEL is required for remote embeddings");
  }

  const response = await fetch(getEmbeddingsUrl(), {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      input: texts,
      model,
    }),
  });

  const rawBody = await response.text();
  let payload = null;
  try {
    payload = rawBody ? JSON.parse(rawBody) : null;
  } catch {
    payload = null;
  }

  if (!response.ok) {
    const message =
      payload?.error?.message ||
      payload?.message ||
      rawBody ||
      `remote embeddings request failed with status ${response.status}`;
    throw new Error(message);
  }

  return normalizeEmbeddingPayload(payload, texts.length);
}

export async function rerankDocumentsViaRemote(
  query,
  documents,
  { model = getDesiredRerankModel() } = {},
) {
  const apiKey = (process.env.OPENAI_API_KEY || "").trim();
  if (!apiKey) {
    throw new Error("OPENAI_API_KEY is required for remote rerank");
  }
  if (!model) {
    throw new Error("RERANK_MODEL is required for remote rerank");
  }

  const response = await fetch(getRerankUrl(), {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model,
      query,
      documents,
      top_n: documents.length,
    }),
  });

  const rawBody = await response.text();
  let payload = null;
  try {
    payload = rawBody ? JSON.parse(rawBody) : null;
  } catch {
    payload = null;
  }

  if (!response.ok) {
    const message =
      payload?.error?.message ||
      payload?.message ||
      rawBody ||
      `remote rerank request failed with status ${response.status}`;
    throw new Error(message);
  }

  return normalizeRerankPayload(payload, documents.length);
}
