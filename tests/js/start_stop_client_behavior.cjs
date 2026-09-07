const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.resolve(__dirname, '../../static/start-stop-client.js'), 'utf8');
const listeners = new Set();
const document = {hidden:false,
  addEventListener(name, fn) {assert.equal(name, 'visibilitychange'); listeners.add(fn);},
  removeEventListener(name, fn) {listeners.delete(fn);}};
const context = {AbortController, DOMException, setTimeout, clearTimeout, document};
vm.createContext(context);
vm.runInContext(source, context);
const client = context.StartStopClient;
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const response = (payload, status=200, type='application/json') => ({ok:status < 400, status,
  headers:{get:()=>type}, json:async()=>payload});

(async () => {
  let calls = 0;
  context.fetch = async (url, options) => {
    calls++;
    assert.equal(options.credentials, 'same-origin');
    assert.equal(options.headers['Content-Type'], 'application/json');
    return response({ok:true});
  };
  assert.equal((await client.request('/api/test', {method:'POST', body:'{}'})).ok, true);
  assert.equal(calls, 1);
  context.fetch = async () => response({error:'配置已被其他人更新'}, 409);
  await assert.rejects(client.request('/api/test'), error => error.status === 409 && /配置/.test(error.message));
  context.fetch = async () => response(null, 200, 'text/html');
  await assert.rejects(client.request('/api/test'), /服务器未返回有效数据/);
  context.fetch = async () => {throw new TypeError('Failed to fetch');};
  await assert.rejects(client.request('/api/test'), /无法连接服务器/);
  // An uncooperative transport cannot leave the UI's loading state hanging.
  context.fetch = () => {calls++; return new Promise(() => {});};
  calls = 0;
  await assert.rejects(client.request('/api/test', {method:'POST', body:'{}', timeoutMs:5}), error => error.name === 'TimeoutError');
  assert.equal(calls, 1, 'a timed out write is never silently retried');
  const controller = new AbortController();
  const cancelled = client.request('/api/test', {signal:controller.signal});
  controller.abort();
  await assert.rejects(cancelled, error => error.name === 'AbortError');
  const before = calls;
  await assert.rejects(client.request('/api/test', {signal:controller.signal}), error => error.name === 'AbortError');
  assert.equal(calls, before, 'pre-cancelled requests do not start a transport');
  context.fetch = async () => ({...response({}), json:()=>new Promise(() => {})});
  await assert.rejects(client.request('/api/test', {timeoutMs:5}), error => error.name === 'TimeoutError');
  context.fetch = async () => ({...response({}), blob:()=>new Promise(() => {})});
  await assert.rejects(client.download('/api/export', {timeoutMs:5}), error => error.name === 'TimeoutError');
  context.fetch = async () => ({...response({}), blob:async()=> 'binary-result'});
  assert.equal((await client.download('/api/export')).blob, 'binary-result');

  let refreshed = 0;
  document.hidden = true;
  const ticket = client.visibleTimeout(() => {refreshed++;}, 1);
  await delay(10);
  assert.equal(refreshed, 0);
  document.hidden = false;
  [...listeners].forEach(fn => fn());
  assert.equal(refreshed, 1);
  assert.equal(listeners.size, 0);
  [...listeners].forEach(fn => fn());
  assert.equal(refreshed, 1);
  document.hidden = true;
  const next = client.visibleTimeout(() => {refreshed++;}, 1);
  await delay(10);
  client.clearVisibleTimeout(next);
  document.hidden = false;
  [...listeners].forEach(fn => fn());
  assert.equal(refreshed, 1);
  assert.equal(listeners.size, 0);
  client.clearVisibleTimeout(ticket);
  const links = new Map();
  client.applyRoutes({querySelectorAll(selector) {const link={}; links.set(selector,link); return [link];}});
  assert.equal(links.size,6);
  assert.equal(links.get('[data-analysis-route]').href, '/start-stop/analysis');
  console.log('PASS: shared request errors, abort, body timeout, no mutation retry, visibility polling, routes');
})().catch(error => {console.error(error); process.exitCode=1;});
