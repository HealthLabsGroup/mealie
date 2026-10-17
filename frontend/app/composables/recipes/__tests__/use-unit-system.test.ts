import { beforeEach, describe, expect, test, vi } from "vitest";
import { useUnitSystem } from "../use-unit-system";

const values = new Map<string, string>();
const storage: Storage = {
  get length() {
    return values.size;
  },
  clear: () => values.clear(),
  getItem: key => values.get(key) ?? null,
  key: index => [...values.keys()][index] ?? null,
  removeItem: key => values.delete(key),
  setItem: (key, value) => values.set(key, value),
};

describe("useUnitSystem", () => {
  beforeEach(() => {
    vi.stubGlobal("localStorage", storage);
    localStorage.clear();
  });

  test("shows recipes as written until the reader opts in", () => {
    const { unitSystem, isConverting } = useUnitSystem();

    expect(unitSystem.value).toBeNull();
    expect(isConverting.value).toBe(false);
  });

  test("nothing is inferred from the locale", () => {
    // deliberately no locale lookup: a wrong guess would silently rewrite every quantity
    expect(useUnitSystem().unitSystem.value).toBeNull();
  });

  test.each(["metric", "us"] as const)("%s can be chosen", (system) => {
    const { unitSystem, isConverting } = useUnitSystem();
    unitSystem.value = system;

    expect(unitSystem.value).toBe(system);
    expect(isConverting.value).toBe(true);
  });

  test("the choice survives a reload", async () => {
    useUnitSystem().unitSystem.value = "us";
    // useStorage writes on flush: "pre", so the value only reaches localStorage on the next tick
    await nextTick();

    expect(localStorage.getItem("recipe-unit-system-preferences")).toContain("us");
    expect(useUnitSystem().unitSystem.value).toBe("us");
  });

  test("showAsWritten goes back to the authored units", () => {
    const { unitSystem, isConverting, showAsWritten } = useUnitSystem();
    unitSystem.value = "metric";
    showAsWritten();

    expect(unitSystem.value).toBeNull();
    expect(isConverting.value).toBe(false);
  });
});
