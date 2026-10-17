import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ref } from "vue";

const canWriteNutrition = ref(false);
const load = vi.fn();
const navigateTo = vi.fn((to: unknown) => to);

vi.mock("~/composables/use-healthlabs-capabilities", () => ({
  useHealthLabsCapabilities: () => ({ canWriteNutrition, load }),
}));

beforeEach(() => {
  canWriteNutrition.value = false;
  load.mockReset();
  navigateTo.mockClear();
  vi.resetModules();
  vi.stubGlobal("defineNuxtRouteMiddleware", (fn: unknown) => fn);
  vi.stubGlobal("navigateTo", navigateTo);
});

afterEach(() => vi.unstubAllGlobals());

async function run() {
  const middleware = (await import("../nutrition-write-only")).default as unknown as () => Promise<unknown>;
  return await middleware();
}

test("redirects users without nutrition write access", async () => {
  await expect(run()).resolves.toBe("/");

  expect(load).toHaveBeenCalledOnce();
  expect(navigateTo).toHaveBeenCalledWith("/");
});

test("allows nutrition editors through", async () => {
  canWriteNutrition.value = true;

  await expect(run()).resolves.toBeUndefined();
  expect(navigateTo).not.toHaveBeenCalled();
});
