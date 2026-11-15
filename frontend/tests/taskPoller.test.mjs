import assert from "node:assert/strict";
import test from "node:test";

import { createTaskPoller } from "../src/utils/taskPoller.ts";

function fakeScheduler() {
  let scheduled;
  return {
    schedule(callback) {
      scheduled = callback;
      return callback;
    },
    cancel(handle) {
      if (scheduled === handle) scheduled = undefined;
    },
    fire() {
      const callback = scheduled;
      scheduled = undefined;
      callback?.();
    },
    get pending() {
      return scheduled !== undefined;
    },
  };
}

const flushMicrotasks = () => new Promise((resolve) => setImmediate(resolve));

test("polls active tasks and stops after the server reports a terminal state", async () => {
  const scheduler = fakeScheduler();
  const results = [];
  let calls = 0;
  const poller = createTaskPoller({
    fetchValue: async () => {
      calls += 1;
      return calls === 1 ? { active: true } : { active: false };
    },
    isActive: (value) => value.active,
    onValue: (value) => results.push(value),
    intervalMs: 1500,
    schedule: scheduler.schedule,
    cancel: scheduler.cancel,
  });

  poller.start();
  await flushMicrotasks();
  assert.equal(calls, 1);
  assert.equal(scheduler.pending, true);

  scheduler.fire();
  await flushMicrotasks();
  assert.equal(calls, 2);
  assert.deepEqual(results, [{ active: true }, { active: false }]);
  assert.equal(scheduler.pending, false);
  poller.stop();
});

test("refresh during an in-flight request discards stale data without overlapping", async () => {
  const scheduler = fakeScheduler();
  const resolvers = [];
  const results = [];
  let calls = 0;
  let activeRequests = 0;
  let maximumActiveRequests = 0;
  const poller = createTaskPoller({
    fetchValue: () => {
      calls += 1;
      activeRequests += 1;
      maximumActiveRequests = Math.max(maximumActiveRequests, activeRequests);
      return new Promise((resolve) => {
        resolvers.push((value) => {
          activeRequests -= 1;
          resolve(value);
        });
      });
    },
    isActive: (value) => value.active,
    onValue: (value) => results.push(value),
    intervalMs: 1500,
    schedule: scheduler.schedule,
    cancel: scheduler.cancel,
  });

  poller.start();
  await flushMicrotasks();
  poller.refresh();
  resolvers[0]({ active: true, progress: 33, version: "stale" });
  await flushMicrotasks();

  assert.equal(calls, 2);
  assert.deepEqual(results, []);
  assert.equal(maximumActiveRequests, 1);

  resolvers[1]({ active: false, progress: 100, version: "fresh" });
  await flushMicrotasks();
  assert.deepEqual(results, [{ active: false, progress: 100, version: "fresh" }]);
  assert.equal(scheduler.pending, false);
  poller.stop();
});

test("stopping the poller prevents late responses from updating state or rescheduling", async () => {
  const scheduler = fakeScheduler();
  const resolvers = [];
  const results = [];
  const poller = createTaskPoller({
    fetchValue: () => new Promise((resolve) => resolvers.push(resolve)),
    isActive: () => true,
    onValue: (value) => results.push(value),
    intervalMs: 1500,
    schedule: scheduler.schedule,
    cancel: scheduler.cancel,
  });

  poller.start();
  await flushMicrotasks();
  poller.stop();
  resolvers[0]({ active: true });
  await flushMicrotasks();

  assert.deepEqual(results, []);
  assert.equal(scheduler.pending, false);
});
