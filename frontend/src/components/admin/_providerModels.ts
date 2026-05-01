/**
 * Curated model catalogs per LLM provider — used by both
 * `PersonasCard` and `SystemTasksCard` to drive the model dropdown.
 * Update as providers ship new models. If a row is currently set to
 * a model not in this list (legacy override or a brand-new model),
 * the Row component prepends it to the dropdown so the value remains
 * selectable.
 *
 * Co-located here rather than in a top-level constants file because
 * only the admin cards reference it.
 */
export const PROVIDER_MODELS: Record<string, string[]> = {
  openai:    ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo", "o1", "o1-mini", "o3-mini"],
  anthropic: ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-sonnet-4-5-20250929", "claude-opus-4-7"],
  gemini:    ["gemini-2.0-flash", "gemini-2.0-flash-exp", "gemini-1.5-pro", "gemini-1.5-flash", "gemini-1.5-flash-8b"],
  // Ollama models depend on what the operator has `ollama pull`-ed locally;
  // these are popular community defaults.
  ollama:    ["llama3.2", "llama3.3:70b", "qwen2.5:14b", "qwen2.5:72b", "mistral-nemo", "deepseek-r1:32b", "phi3"],
  minimax:   ["MiniMax-M2.7", "MiniMax-M2.7-highspeed", "abab6.5s-chat", "abab6.5-chat", "MiniMax-Text-01"],
  groq:      ["llama-3.3-70b-versatile", "llama-3.1-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"],
  deepseek:  ["deepseek-chat", "deepseek-reasoner"],
  // OpenRouter has 100+ models; pick a curated set covering the major
  // model families. Power users editing a route to something exotic still
  // see their custom value (prepended) — they just can't browse the long
  // tail from this UI.
  openrouter: [
    "openai/gpt-4o", "openai/gpt-4o-mini",
    "anthropic/claude-3.5-sonnet", "anthropic/claude-3-opus",
    "google/gemini-pro-1.5", "google/gemini-flash-1.5",
    "meta-llama/llama-3.1-70b-instruct", "mistralai/mixtral-8x22b-instruct",
    "deepseek/deepseek-chat", "qwen/qwen-2.5-72b-instruct",
  ],
  // claude_agent uses the Claude Agent SDK which under the hood talks to
  // anthropic's Claude models — same catalog.
  claude_agent: ["claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001", "claude-opus-4-7", "claude-sonnet-4-6"],
};

export const VALID_PROVIDERS = Object.keys(PROVIDER_MODELS);
