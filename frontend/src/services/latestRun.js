export function isLatestRunMessage(messages, message) {
  return Boolean(message?.runId) && messages?.[messages.length - 1] === message;
}
