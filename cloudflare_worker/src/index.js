const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
};

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: JSON_HEADERS,
  });
}

function findUrl(value) {
  if (!value) return "";
  if (typeof value === "string" && /^https?:\/\//i.test(value)) return value;
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = findUrl(item);
      if (found) return found;
    }
    return "";
  }
  if (typeof value === "object") {
    for (const key of [
      "directLink",
      "downloadUrl",
      "url",
      "link",
      "directlink",
    ]) {
      const found = findUrl(value[key]);
      if (found) return found;
    }
    for (const item of Object.values(value)) {
      const found = findUrl(item);
      if (found) return found;
    }
  }
  return "";
}

async function getWebToken() {
  const response = await fetch("https://gofile.io/dist/js/global.js");
  if (!response.ok) {
    throw new Error(`Gofile global.js returned ${response.status}`);
  }
  const script = await response.text();
  const match = script.match(/appdata\.wt\s*=\s*['"]([^'"]+)['"]/);
  if (!match) {
    throw new Error("Gofile web token was not found");
  }
  return match[1];
}

function gofileTokens(env) {
  return String(env.GOFILE_API_TOKENS || env.GOFILE_API_TOKEN || "")
    .split(",")
    .map((token) => token.trim())
    .filter(Boolean);
}

async function getFileLink(folderId, fileId, token, wt) {
  const params = new URLSearchParams({
    wt,
    contentFilter: "",
    page: "1",
    pageSize: "1000",
    sortField: "createTime",
    sortDirection: "-1",
  });
  const response = await fetch(
    `https://api.gofile.io/contents/${encodeURIComponent(folderId)}?${params}`,
    {
      headers: { Authorization: `Bearer ${token}` },
    },
  );
  if (!response.ok) {
    throw new Error(`Gofile contents API returned ${response.status}`);
  }
  const payload = await response.json();
  if (payload.status && payload.status !== "ok") {
    throw new Error(`Gofile contents API returned ${payload.status}`);
  }
  const children = payload.data?.children;
  if (!children || typeof children !== "object") {
    throw new Error("Gofile folder response has no children");
  }
  const child = children[fileId] ||
    Object.values(children).find((item) => item?.id === fileId);
  const link = child?.link || child?.directLink || child?.downloadUrl;
  if (!link) {
    throw new Error("Gofile file link was not found in folder contents");
  }
  return link;
}

async function getGofileDirectLink(folderId, fileId, env) {
  const tokens = gofileTokens(env);
  if (!tokens.length) throw new Error("No Gofile API token configured");
  const wt = await getWebToken();
  let lastError;
  for (const token of tokens) {
    try {
      return await getFileLink(folderId, fileId, token, wt);
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError || new Error("Gofile file lookup failed");
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const match = url.pathname.match(/^\/download\/([^/]+)\/([^/]+)$/);
    if (request.method !== "GET" || !match) {
      return json({ error: "Not found" }, 404);
    }

    if (!gofileTokens(env).length) {
      return json({ error: "Worker is not configured" }, 500);
    }
    if (
      env.WORKER_ACCESS_KEY &&
      url.searchParams.get("key") !== env.WORKER_ACCESS_KEY
    ) {
      return json({ error: "Unauthorized" }, 401);
    }

    try {
      const directLink = await getGofileDirectLink(match[1], match[2], env);
      const upstream = await fetch(directLink, {
        headers: {
          Range: request.headers.get("Range") || "",
        },
      });
      if (!upstream.ok && upstream.status !== 206) {
        return new Response("Upstream download failed", {
          status: upstream.status,
          headers: { "content-type": "text/plain; charset=utf-8" },
        });
      }

      const headers = new Headers(upstream.headers);
      headers.set("cache-control", "private, no-store");
      headers.delete("set-cookie");
      return new Response(upstream.body, {
        status: upstream.status,
        headers,
      });
    } catch (error) {
      console.error(error);
      return json({ error: "Download is temporarily unavailable" }, 502);
    }
  },
};