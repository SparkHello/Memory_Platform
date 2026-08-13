/** Compare OpenAI-compatible channel URLs without trailing slashes. */
export function channelUrlKey(url: string): string {
  return url.trim().replace(/\/+$/, "");
}

/** Return the embedding URL to persist, or undefined when it matches chat. */
export function distinctEmbeddingBaseUrl(chatUrl: string, embeddingUrl: string): string | undefined {
  const embed = channelUrlKey(embeddingUrl);
  if (!embed || embed === channelUrlKey(chatUrl)) return undefined;
  return embed;
}
