import { computed, ref } from "vue";
import { useUserApi } from "~/composables/api";
import type { HealthLabsCapability } from "~/lib/api/types/healthlabs";

const capabilities = ref<HealthLabsCapability[]>([]);
const loadedUserId = ref<string | null>(null);
let capabilityLoadId = 0;

export function resetHealthLabsCapabilities() {
  capabilityLoadId++;
  capabilities.value = [];
  loadedUserId.value = null;
}

export function useHealthLabsCapabilities() {
  const auth = useMealieAuth();
  const api = useUserApi();

  async function load(force = false) {
    const loadId = ++capabilityLoadId;
    const userId = auth.user.value?.id ?? null;
    if (!userId) {
      capabilities.value = [];
      loadedUserId.value = null;
      return;
    }
    if (!force && loadedUserId.value === userId) {
      return;
    }

    capabilities.value = [];
    loadedUserId.value = null;
    const { data } = await api.healthlabsNutrition.getCapabilities();
    if (loadId !== capabilityLoadId || auth.user.value?.id !== userId) {
      return;
    }
    capabilities.value = data ?? [];
    loadedUserId.value = data ? userId : null;
  }

  return {
    capabilities,
    canWriteNutrition: computed(() => (
      loadedUserId.value !== null
      && loadedUserId.value === auth.user.value?.id
      && capabilities.value.includes("nutrition:write")
    )),
    load,
  };
}
