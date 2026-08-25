const LOCAL_ARTIFACT_LINK = /\[[^\]\r\n]*\]\(\s*<?(?:file:\/\/)?\/var\/lib\/xuanshu\/workspaces\/[^\r\n>)]*>?\s*\)/g;

export function stripLocalArtifactReferences(value = "", options = {}) {
  const result = String(value)
    .replace(LOCAL_ARTIFACT_LINK, "")
    .replace(/^[ \t]*(?:[-*]\s*)?$/gm, "")
    .replace(/\n{3,}/g, "\n\n");
  return options.trim === false ? result : result.trim();
}

function escapeHtml(value = "") {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function safeUrl(value = "") {
  const decoded = String(value).replace(/&amp;/g, "&");
  if (/^(https?:\/\/|\/)/i.test(decoded)) return escapeHtml(decoded);
  return "";
}

function inlineMarkdown(value = "") {
  return value
    .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (_, alt, url) => {
      const src = safeUrl(url);
      return src ? `<img class="rich-message-image" src="${src}" alt="${alt}" loading="lazy">` : `![${alt}](${url})`;
    })
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, url) => {
      const href = safeUrl(url);
      return href ? `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>` : label;
    })
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>")
    .replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
}

function tableHtml(lines) {
  if (lines.length < 2 || !/^\s*\|?\s*:?-{3,}/.test(lines[1])) return "";
  const cells = (line) => line.replace(/^\s*\||\|\s*$/g, "").split("|").map((item) => item.trim());
  const headers = cells(lines[0]);
  const separators = cells(lines[1]);
  if (!headers.length || headers.length !== separators.length || separators.some((item) => !/^:?-{3,}:?$/.test(item))) return "";
  const rows = lines.slice(2).map(cells);
  return `<div class="rich-table-wrap"><table><thead><tr>${headers.map((item) => `<th>${inlineMarkdown(item)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${headers.map((_, index) => `<td>${inlineMarkdown(row[index] || "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

export function renderRichMessage(value = "") {
  const escaped = escapeHtml(stripLocalArtifactReferences(value));
  const tokens = escaped.split(/(```[^\n]*\n[\s\S]*?```)/g).filter(Boolean);
  return tokens.map((token) => {
    const fenced = token.match(/^```[^\n]*\n([\s\S]*?)\n?```$/);
    if (fenced) return `<pre><code>${fenced[1]}</code></pre>`;
    return token.split(/\n{2,}/).filter((block) => block.trim()).map((block) => {
      const lines = block.split("\n");
      const table = tableHtml(lines);
      if (table) return table;
      if (lines.every((line) => /^[-*] /.test(line)))
        return `<ul>${lines.map((line) => `<li>${inlineMarkdown(line.slice(2))}</li>`).join("")}</ul>`;
      if (lines.every((line) => /^\d+\. /.test(line)))
        return `<ol>${lines.map((line) => `<li>${inlineMarkdown(line.replace(/^\d+\. /, ""))}</li>`).join("")}</ol>`;
      const heading = block.match(/^(#{1,4}) (.+)$/);
      if (heading) return `<h${Math.min(6, heading[1].length + 2)}>${inlineMarkdown(heading[2])}</h${Math.min(6, heading[1].length + 2)}>`;
      return `<p>${inlineMarkdown(block).replace(/\n/g, "<br>")}</p>`;
    }).join("");
  }).join("");
}
