type TimerHandle = ReturnType<typeof setTimeout>;

export interface TaskPollerOptions<T> {
  fetchValue: () => Promise<T>;
  isActive: (value: T) => boolean;
  onValue: (value: T) => void;
  onError?: (error: unknown) => void;
  intervalMs: number;
  schedule?: (callback: () => void, delay: number) => TimerHandle;
  cancel?: (handle: TimerHandle) => void;
}

export interface TaskPoller {
  start: () => void;
  refresh: () => void;
  stop: () => void;
}

export function createTaskPoller<T>(options: TaskPollerOptions<T>): TaskPoller {
  const schedule = options.schedule ?? ((callback, delay) => setTimeout(callback, delay));
  const cancel = options.cancel ?? ((handle) => clearTimeout(handle));
  let timer: TimerHandle | undefined;
  let started = false;
  let inFlight = false;
  let refreshQueued = false;
  let generation = 0;

  function clearTimer() {
    if (timer !== undefined) cancel(timer);
    timer = undefined;
  }

  function scheduleNext() {
    clearTimer();
    if (!started) return;
    timer = schedule(() => {
      timer = undefined;
      void poll();
    }, options.intervalMs);
  }

  async function poll() {
    if (!started) return;
    if (inFlight) {
      refreshQueued = true;
      return;
    }

    inFlight = true;
    try {
      do {
        refreshQueued = false;
        const requestGeneration = generation;
        let value: T;
        try {
          value = await options.fetchValue();
        } catch (error) {
          if (!started) return;
          if (refreshQueued) continue;
          options.onError?.(error);
          scheduleNext();
          return;
        }

        if (!started) return;
        if (requestGeneration !== generation) {
          refreshQueued = true;
          continue;
        }

        options.onValue(value);
        if (options.isActive(value)) scheduleNext();
        else clearTimer();
      } while (refreshQueued && started);
    } finally {
      inFlight = false;
    }
  }

  return {
    start() {
      if (started) return;
      started = true;
      generation += 1;
      void poll();
    },
    refresh() {
      if (!started) return;
      generation += 1;
      clearTimer();
      if (inFlight) refreshQueued = true;
      else void poll();
    },
    stop() {
      started = false;
      generation += 1;
      refreshQueued = false;
      clearTimer();
    },
  };
}
