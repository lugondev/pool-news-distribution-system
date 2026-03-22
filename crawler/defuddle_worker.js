#!/usr/bin/env node
/**
 * Defuddle worker — reads {url, html} from stdin, writes {content, title, description} to stdout.
 * Called by crawler/content_extractor.py via asyncio subprocess.
 */
const Defuddle = require("defuddle");
const { JSDOM } = require("jsdom");

const chunks = [];
process.stdin.on("data", (chunk) => chunks.push(chunk));
process.stdin.on("end", () => {
  try {
    const { url, html } = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    const dom = new JSDOM(html, { url });
    const result = new Defuddle(dom.window.document, { url }).parse();

    process.stdout.write(
      JSON.stringify({
        content: result.content || "",
        title: result.title || "",
        description: result.description || "",
        author: result.author || "",
        published: result.published || "",
      })
    );
  } catch (e) {
    process.stdout.write(
      JSON.stringify({ error: e.message, content: "", title: "", description: "" })
    );
  }
});
