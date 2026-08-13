/** IDs that belong in the embedding picker, not the chat-model dropdown. */
const NON_CHAT_MODEL_RE =
  /text-embedding|embedding|\/embed(?:ding)?\b|-embed(?:ding)?\b|\btts\b|\basr\b|whisper|speech-\d|image-edit|image-plus|qwen-image|wan2\.|wanx/i;

export function isLikelyChatModelId(modelId: string): boolean {
  return Boolean(modelId.trim()) && !NON_CHAT_MODEL_RE.test(modelId);
}

export function filterDiscoveredChatModels<T extends { id: string; aliases?: string[] }>(
  models: readonly T[],
  query = ""
): T[] {
  const chatModels = models.filter((model) => isLikelyChatModelId(model.id));
  const listed = chatModels.length > 0 ? chatModels : [...models];
  const needle = query.trim().toLowerCase();
  if (!needle) return listed;
  return listed.filter((model) => {
    if (model.id.toLowerCase().includes(needle)) return true;
    return (model.aliases || []).some((alias) => alias.toLowerCase().includes(needle));
  });
}
