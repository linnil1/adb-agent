// Run with: node scripts/test_viewer.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
let config;
vm.runInNewContext(html.match(/<script>\s*([\s\S]*?)<\/script>/)[1], {
  Vue: {createApp(value) { config = value; return {mount() {}}; }}
});
const app = {...config.data()};
for (const [name, method] of Object.entries(config.methods)) app[name] = method.bind(app);

const description = {
  target: 'device-a',
  elements: [{label: 'OK', bounds: [10, 20, 100, 200], states: ['clickable']}]
};
app.applyViewerEvent({type: 'description', ...description});
assert.equal(app.screens['device-a'].ready, true);
assert.equal(app.screens['device-a'].url, '');

// Explicit and interactive screenshots must preserve the existing overlay.
for (const interactive of [false, true]) {
  app.applyViewerEvent({type: 'screenshot', target: 'device-a', revision: 1, interactive});
  assert.equal(app.screens['device-a'].descriptionElements, description.elements);
  app.screenLoaded('device-a', {currentTarget: {naturalWidth: 1080, naturalHeight: 1920}});
  assert.equal(app.screens['device-a'].ready, true);
  assert.equal(app.screens['device-a'].height, 1920);
  assert.equal(app.screens['device-a'].descriptionElements, description.elements);
}

app.applyViewerEvent({type: 'state', screenshots: [{target: 'device-a', revision: 1}], descriptions: [description]});
app.screenLoaded('device-a', {currentTarget: {naturalWidth: 1080, naturalHeight: 1920}});
assert.equal(app.screens['device-a'].descriptionElements, description.elements);

app.clearScreen('device-a', 'screenshot');
assert.equal(app.screens['device-a'].url, '');
assert.equal(app.screens['device-a'].ready, true);
assert.equal(app.screens['device-a'].descriptionElements, description.elements);
app.clearScreen('device-a', 'description');
assert.equal(app.screens['device-a'].descriptionElements.length, 0);
assert.equal(app.screens['device-a'].ready, false);
console.log('Passed: description survives explicit/interactive screenshots and reconnect; cleanup still works.');
