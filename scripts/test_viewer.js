// Run with: node scripts/test_viewer.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
let config;
vm.runInNewContext(html.match(/<script>\s*([\s\S]*?)<\/script>/)[1], {
  Vue: {createApp(value) { config = value; return {mount() {}}; }},
  setTimeout, clearTimeout, setInterval, clearInterval
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

// A crop sits over the last full screenshot, positioned in full-screen coordinates.
const full = {currentTarget: {naturalWidth: 1080, naturalHeight: 1920}};
app.applyViewerEvent({type: 'screenshot', target: 'device-b', revision: 10, interactive: false});
app.screenLoaded('device-b', full);
const crop = {type: 'screenshot', target: 'device-b', revision: 11, interactive: false, region: [108, 192, 540, 960], screen: [1080, 1920]};
app.applyViewerEvent(crop);
assert.match(app.screens['device-b'].url, /revision=10/);
assert.match(app.screens['device-b'].crop.url, /revision=11/);
assert.deepEqual({...app.cropStyle('device-b')}, {left: '10%', top: '10%', width: '40%', height: '40%'});

// Clicks map to the full screen even though the crop image is only 432x768.
const surface = {getBoundingClientRect: () => ({left: 0, top: 0, right: 540, bottom: 960, width: 540, height: 960})};
const click = {currentTarget: {querySelector: selector => selector === '.screen-surface' ? surface : null}, clientX: 270, clientY: 480};
assert.deepEqual({...app.screenPoint('device-b', click)}, {x: 540, y: 960, clientX: 270, clientY: 480});

// Gestures use the full screen size too.
app.showGesture({target: 'device-b', gesture: 'tap', x: 540, y: 960});
assert.equal(app.screens['device-b'].effect.x, 50);
assert.equal(app.screens['device-b'].effect.y, 50);

// The next full screenshot replaces the crop.
app.applyViewerEvent({type: 'screenshot', target: 'device-b', revision: 12, interactive: true});
assert.equal(app.screens['device-b'].crop, null);

// Reconnecting restores both layers; without a full screenshot the crop sets the screen size.
app.applyViewerEvent({type: 'state', screenshots: [{target: 'device-b', revision: 10}], crops: [crop], descriptions: []});
assert.match(app.screens['device-b'].crop.url, /revision=11/);
app.clearScreen('device-b', 'screenshot');
assert.equal(app.screens['device-b'].crop, null);
app.applyViewerEvent(crop);
assert.equal(app.screens['device-b'].url, '');
assert.equal(app.screens['device-b'].ready, true);
assert.equal(app.screens['device-b'].width, 1080);
assert.equal(app.screens['device-b'].height, 1920);

// A full screenshot of another size (e.g. rotated) is dropped from under the crop.
app.applyViewerEvent({type: 'state', screenshots: [{target: 'device-b', revision: 10}], crops: [crop], descriptions: []});
app.screenLoaded('device-b', {currentTarget: {naturalWidth: 1920, naturalHeight: 1080}});
assert.equal(app.screens['device-b'].url, '');
assert.equal(app.screens['device-b'].width, 1080);

// Optional integer tool fields are sent as numbers.
assert.equal(app.numeric({anyOf: [{type: 'integer'}, {type: 'null'}]}), true);
assert.deepEqual({...app.argumentsFor({inputSchema: {properties: {x1: {anyOf: [{type: 'integer'}, {type: 'null'}]}}}}, {x1: '108'})}, {x1: 108});
console.log('Passed: crops overlay the full screenshot, map clicks to full-screen coordinates, and are replaced by full screenshots.');

// Recording: a REC badge while running, then a playable MP4 panel.
const started = {type: 'recording', state: 'started', target: 'device-c', started_at: new Date().toISOString(), time_limit: 30};
app.applyViewerEvent(started);
assert.equal(app.activeRecordings['device-c'].time_limit, 30);
app.applyViewerEvent({type: 'recording', state: 'stopped', target: 'device-c', revision: 3, seconds: 4.58, bytes: 1200000, captured_at: new Date().toISOString()});
assert.equal(app.activeRecordings['device-c'], undefined);
assert.equal(app.recordings['device-c'].url, '/api/viewer/recording?target=device-c&revision=3');
assert.match(app.recordingLabel('device-c'), /^4\.6 s · 1\.2 MB · /);
app.applyViewerEvent({type: 'cleared', target: 'device-c', kind: 'recording'});
assert.equal(app.recordings['device-c'], undefined);

// A failed stop clears the badge without a video; reconnect skips recordings past their limit.
app.applyViewerEvent(started);
app.applyViewerEvent({type: 'recording', state: 'idle', target: 'device-c'});
assert.equal(app.activeRecordings['device-c'], undefined);
app.applyViewerEvent({type: 'state', screenshots: [], descriptions: [], recordings: [{target: 'device-c', revision: 4, seconds: 1, bytes: 1}], active_recordings: [{...started, started_at: '2000-01-01T00:00:00Z'}]});
assert.equal(app.recordings['device-c'].revision, 4);
assert.match(app.recordingLabel('device-c'), /^1\.0 s · 1 KB · /);
assert.equal(app.activeRecordings['device-c'], undefined);
for (const timer of Object.values(app.recordingTimers)) clearTimeout(timer);
console.log('Passed: recordings show a REC badge while running and a playable MP4 when stopped.');
