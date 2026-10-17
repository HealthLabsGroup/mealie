import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ref } from "vue";

const getCapabilities = vi.fn();
const user = ref<{ id: string } | null>({ id: "editor-1" });

vi.mock("~/composables/api", () => ({
  useUserApi: () => ({ healthlabsNutrition: { getCapabilities } }),
}));

beforeEach(() => {
  getCapabilities.mockReset();
  user.value = { id: "editor-1" };
  vi.stubGlobal("useMealieAuth", () => ({ user }));
  vi.resetModules();
});

afterEach(() => vi.unstubAllGlobals());

test("reloads capabilities when the signed-in user changes", async () => {
  getCapabilities
    .mockResolvedValueOnce({ data: ["nutrition:write"], error: null })
    .mockResolvedValueOnce({ data: [], error: null });
  const { useHealthLabsCapabilities } = await import("../use-healthlabs-capabilities");
  const access = useHealthLabsCapabilities();

  await access.load();
  expect(access.canWriteNutrition.value).toBe(true);

  user.value = { id: "user-2" };
  expect(access.canWriteNutrition.value).toBe(false);
  await access.load();

  expect(access.canWriteNutrition.value).toBe(false);
  expect(getCapabilities).toHaveBeenCalledTimes(2);
});

test("does not grant access when capability discovery fails", async () => {
  getCapabilities.mockResolvedValue({ data: null, error: new Error("offline") });
  const { useHealthLabsCapabilities } = await import("../use-healthlabs-capabilities");

  const access = useHealthLabsCapabilities();
  await access.load();

  expect(access.canWriteNutrition.value).toBe(false);
});

test("clears cached capabilities on logout", async () => {
  getCapabilities.mockResolvedValue({ data: ["nutrition:write"], error: null });
  const { resetHealthLabsCapabilities, useHealthLabsCapabilities } = await import("../use-healthlabs-capabilities");
  const access = useHealthLabsCapabilities();
  await access.load();

  resetHealthLabsCapabilities();

  expect(access.canWriteNutrition.value).toBe(false);
});

test("discards an older capability response for the same user", async () => {
  let resolveFirst!: (value: unknown) => void;
  let resolveSecond!: (value: unknown) => void;
  getCapabilities
    .mockReturnValueOnce(new Promise(resolve => resolveFirst = resolve))
    .mockReturnValueOnce(new Promise(resolve => resolveSecond = resolve));
  const { useHealthLabsCapabilities } = await import("../use-healthlabs-capabilities");
  const access = useHealthLabsCapabilities();

  const firstLoad = access.load();
  const secondLoad = access.load(true);
  resolveSecond({ data: [], error: null });
  await secondLoad;
  resolveFirst({ data: ["nutrition:write"], error: null });
  await firstLoad;

  expect(access.canWriteNutrition.value).toBe(false);
});

test("discards a capability response started before logout", async () => {
  let resolveLoad!: (value: unknown) => void;
  getCapabilities.mockReturnValueOnce(new Promise(resolve => resolveLoad = resolve));
  const { resetHealthLabsCapabilities, useHealthLabsCapabilities } = await import("../use-healthlabs-capabilities");
  const access = useHealthLabsCapabilities();

  const pendingLoad = access.load();
  resetHealthLabsCapabilities();
  resolveLoad({ data: ["nutrition:write"], error: null });
  await pendingLoad;

  expect(access.canWriteNutrition.value).toBe(false);
});
