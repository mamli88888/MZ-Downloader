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

async function getGofileDirectLink(contentId, token) {
  const response = await fetch(
    `https://api.gofile.io/contents/${encodeURIComponent(contentId)}/directlinks`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "content-type": "application/json",
      },
      body: JSON.stringify({}),
    },
  );
  if (!response.ok) {
    throw new Error(`Gofile direct-link API returned ${response.status}`);
  }
  const payload = await response.json();
  const directLink = findUrl(payload);
  if (!directLink) {
    throw new Error("Gofile response did not include a direct link");
  }
  return directLink;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const match = url.pathname.match(/^\/download\/([^/]+)$/);
    if (request.method !== "GET" || !match) {
      return json({ error: "Not found" }, 404);
    }

    if (!env.GOFILE_API_TOKEN) {
      return json({ error: "Worker is not configured" }, 500);
    }
    if (
      env.WORKER_ACCESS_KEY &&
      url.searchParams.get("key") !== env.WORKER_ACCESS_KEY
    ) {
      return json({ error: "Unauthorized" }, 401);
    }

    try {
      const directLink = await getGofileDirectLink(match[1], env.GOFILE_API_TOKEN);
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