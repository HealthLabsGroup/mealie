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
