#!/usr/bin/env node
// Pin every image tag in values.yaml with its registry digest.
//
// Renovate bumps image tags via a custom manager and intentionally drops the
// digest when updating the version (the old digest would point to the previous
// image). This Renovate post-upgrade task re-pins fresh digests for every tag
// in the chart's values.yaml that has none.
//
// It only calls the registry for tags without a digest, so it stays cheap even
// when triggered by unrelated upgrades of the same chart.
//
// The repository of a tag is the closest 'repository:' line above it, which
// matches how image blocks are laid out in these charts.
//
// Usage: node scripts/sync-image-digest.js <chart-dir>
import { readFileSync, writeFileSync } from "node:fs";

const ACCEPT = [
  "application/vnd.oci.image.index.v1+json",
  "application/vnd.docker.distribution.manifest.list.v2+json",
  "application/vnd.oci.image.manifest.v1+json",
  "application/vnd.docker.distribution.manifest.v2+json",
].join(", ");

function fail(message) {
  console.error(message);
  process.exit(1);
}

async function resolveDigest(repository, tag) {
  const firstSegment = repository.split("/")[0];
  if (firstSegment.includes(".") && !["ghcr.io", "docker.io"].includes(firstSegment)) {
    throw new Error(`Unsupported registry: ${firstSegment}`);
  }

  let tokenUrl, manifestUrl;
  if (repository.startsWith("ghcr.io/")) {
    const path = repository.slice("ghcr.io/".length);
    tokenUrl = `https://ghcr.io/token?scope=repository:${path}:pull`;
    manifestUrl = `https://ghcr.io/v2/${path}/manifests/${tag}`;
  } else {
    let path = repository;
    if (repository.startsWith("docker.io/")) {
      path = repository.slice("docker.io/".length);
    }
    if (!path.includes("/")) {
      path = `library/${path}`;
    }
    tokenUrl = `https://auth.docker.io/token?service=registry.docker.io&scope=repository:${path}:pull`;
    manifestUrl = `https://registry-1.docker.io/v2/${path}/manifests/${tag}`;
  }

  const token = (await (await fetch(tokenUrl)).json()).token;
  const response = await fetch(manifestUrl, {
    headers: { Accept: ACCEPT, Authorization: `Bearer ${token}` },
  });
  if (!response.ok) {
    throw new Error(`Registry returned ${response.status} for ${manifestUrl}`);
  }
  const digest = response.headers.get("docker-content-digest");
  if (!digest || !/^sha256:[a-f0-9]{64}$/.test(digest)) {
    throw new Error("Registry returned no valid docker-content-digest header");
  }
  return digest;
}

function unquote(value) {
  return value.replace(/^["']|["']$/g, "").trim();
}

async function main() {
  const parentDir = process.argv[2];
  if (!parentDir) {
    fail("Usage: sync-image-digest.js <chart-dir>");
  }
  const valuesFile = `charts/${parentDir}/values.yaml`;
  const content = readFileSync(valuesFile, "utf8");
  const lines = content.split("\n");

  // Collect every tag that has no digest, paired with the closest repository
  // declared above it.
  let repository = "";
  const unpinned = [];
  for (const [index, line] of lines.entries()) {
    const repositoryMatch = line.match(/^[ \t]*repository:[ \t]*(.+)$/);
    if (repositoryMatch) {
      repository = unquote(repositoryMatch[1]);
      continue;
    }
    const tagMatch = line.match(/^[ \t]*tag:[ \t]*(.*)$/);
    if (tagMatch) {
      const tag = unquote(tagMatch[1]);
      if (!tag || tag.includes("@sha256:")) {
        continue; // empty or already pinned (no registry call needed)
      }
      if (!repository) {
        fail(`No repository found above the unpinned tag on line ${index + 1} of ${valuesFile}`);
      }
      unpinned.push({ index, repository, tag });
    }
  }

  if (unpinned.length === 0) {
    console.log(`All image tags in ${valuesFile} are pinned to digests`);
    return;
  }

  // Resolve everything before writing anything, so a failed lookup leaves the
  // file untouched.
  for (const entry of unpinned) {
    console.log(`Pinning ${entry.repository}:${entry.tag}`);
    entry.digest = await resolveDigest(entry.repository, entry.tag);
  }

  for (const entry of unpinned) {
    const indent = lines[entry.index].match(/^[ \t]*/)[0];
    lines[entry.index] = `${indent}tag: "${entry.tag}@${entry.digest}"`;
    console.log(`Pinned ${entry.repository}:${entry.tag} to ${entry.digest}`);
  }
  writeFileSync(valuesFile, lines.join("\n"));
}

main().catch((error) => {
  console.error(error.message ?? error);
  process.exit(1);
});
