// Optional integration check using Harness's actual pi-ai dependency, not a hand-built SDK request.
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';

const root = process.env.PI_AI_TEST_ROOT;
const api = name => pathToFileURL(join(root, 'node_modules/@earendil-works/pi-ai/dist', name)).href;
const { stream } = await import(api('api/openai-completions.js'));
const { normalizeContext } = await import(api('utils/transcript.js'));
const model = {
  id: 'lab-chat', name: '模拟测试模型', api: 'openai-completions', provider: 'lab-gateway',
  baseUrl: process.env.LAB_LLM_BASE_URL, input: ['text'], reasoning: true,
  contextWindow: 1_000_000, maxTokens: 128_000,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
};
const context = normalizeContext({
  systemPrompt: '只用于隔离模拟测试的系统指令',
  messages: [{ role: 'user', content: '你好', timestamp: 0 }],
});
const checks = [];
for (const effort of ['max', 'xhigh']) {
  const response = await stream(model, context, {
    apiKey: process.env.LAB_LLM_API_KEY,
    maxTokens: 128_000, reasoningEffort: effort, maxRetries: 0,
  }).result();
  assert.equal(response.stopReason, 'stop', response.errorMessage);
  assert.equal(response.content.filter(block => block.type === 'text').map(block => block.text).join(''), 'Hello from mock');
  assert.equal(response.usage.output, 20);
  checks.push({ effort, stopReason: response.stopReason, outputTokens: response.usage.output });
}
console.log(JSON.stringify({ checks, dependency: '@earendil-works/pi-ai@1.3.0' }));
