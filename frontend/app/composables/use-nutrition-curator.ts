import { computed, ref } from "vue";
import { useUserApi } from "~/composables/api";
import type {
  HealthLabsFoodNutritionOut,
  HealthLabsFoodPortionCreate,
  HealthLabsNutrientFoodOut,
  HealthLabsNutritionWorkerStatus,
  HealthLabsNutritionWorkOut,
} from "~/lib/api/types/healthlabs";

export type NutritionQueueFilter = "needs-review" | "matched" | "all";

export function useNutritionCurator() {
  const api = useUserApi().healthlabsNutrition;
  const foods = ref<HealthLabsFoodNutritionOut[]>([]);
  const total = ref(0);
  const selectedFood = ref<HealthLabsFoodNutritionOut | null>(null);
  const candidates = ref<HealthLabsNutrientFoodOut[]>([]);
  const workerStatus = ref<HealthLabsNutritionWorkerStatus | null>(null);
  const reviewWork = ref<HealthLabsNutritionWorkOut[]>([]);
  const country = ref("GB");
  const queueQuery = ref<string | null>("");
  const sourceQuery = ref("");
  const queueFilter = ref<NutritionQueueFilter>("needs-review");
  const loadingFoods = ref(false);
  const searchingSources = ref(false);
  const saving = ref(false);
  const loadingWork = ref(false);
  const foodError = ref<string | null>(null);
  const sourceError = ref<string | null>(null);
  const actionError = ref<string | null>(null);
  const workError = ref<string | null>(null);
  let foodLoadId = 0;
  let sourceSearchId = 0;
  let confirmedCountry = country.value;
  const confirmedUpdates = new Map<string, { food: HealthLabsFoodNutritionOut; afterLoadId: number }>();

  const visibleFoods = computed(() => foods.value.filter((food) => {
    if (queueFilter.value === "all") {
      return true;
    }
    if (queueFilter.value === "matched") {
      return food.status === "matched";
    }
    return food.status === "unknown" || food.status === "review_required";
  }));

  async function loadFoods() {
    const loadId = ++foodLoadId;
    const loadCountry = country.value;
    if (loadCountry !== confirmedCountry) {
      confirmedUpdates.clear();
      confirmedCountry = loadCountry;
    }
    const q = (queueQuery.value ?? "").trim();
    loadingFoods.value = true;
    foodError.value = null;
    try {
      const { data, error } = await api.getFoods({
        country: loadCountry,
        q: q || undefined,
      });
      if (loadId !== foodLoadId || country.value !== loadCountry || (queueQuery.value ?? "").trim() !== q) {
        return false;
      }
      if (error || !data) {
        foodError.value = "Unable to load foods.";
        return false;
      }
      foods.value = data.items.map((food) => {
        const confirmed = confirmedUpdates.get(food.id);
        if (confirmed?.afterLoadId >= loadId) {
          return confirmed.food;
        }
        confirmedUpdates.delete(food.id);
        return food;
      });
      total.value = data.total;
      const refreshedSelection = foods.value.find(food => food.id === selectedFood.value?.id);
      if (refreshedSelection) {
        selectedFood.value = refreshedSelection;
      }
      return true;
    }
    finally {
      if (loadId === foodLoadId) {
        loadingFoods.value = false;
      }
    }
  }

  async function loadWorker() {
    loadingWork.value = true;
    workError.value = null;
    try {
      const [statusResponse, ingredientResponse, foodResponse, portionResponse] = await Promise.all([
        api.getWorkerStatus(),
        api.getWork({ state: "review_required", kind: "ingredient_structure" }),
        api.getWork({ state: "review_required", kind: "food_match" }),
        api.getWork({ state: "review_required", kind: "portion_resolution" }),
      ]);
      const workResponses = [ingredientResponse, foodResponse, portionResponse];
      if (statusResponse.data) {
        workerStatus.value = statusResponse.data;
      }
      if (workResponses.every(response => response.data && !response.error)) {
        reviewWork.value = workResponses.flatMap(response => response.data ?? []);
      }
      if (
        statusResponse.error
        || !statusResponse.data
        || workResponses.some(response => response.error || !response.data)
      ) {
        workError.value = "Unable to refresh background nutrition work.";
        return false;
      }
      return true;
    }
    finally {
      loadingWork.value = false;
    }
  }

  async function openWork(work: HealthLabsNutritionWorkOut) {
    const foodId = typeof work.result?.food_id === "string" ? work.result.food_id : null;
    if (!foodId) {
      actionError.value = "This review item no longer has a food target.";
      return false;
    }
    let food = foods.value.find(item => item.id === foodId);
    if (!food) {
      await loadFoods();
      food = foods.value.find(item => item.id === foodId);
    }
    if (!food) {
      actionError.value = "The food for this review item is not in the current queue.";
      return false;
    }
    await selectFood(food);
    return true;
  }

  async function selectFood(food: HealthLabsFoodNutritionOut) {
    selectedFood.value = food;
    sourceQuery.value = food.name?.trim() ?? "";
    sourceError.value = null;
    candidates.value = [];
    await searchSources();
  }

  async function searchSources() {
    const searchId = ++sourceSearchId;
    const foodId = selectedFood.value?.id ?? null;
    const q = sourceQuery.value.trim();
    const searchCountry = country.value;
    sourceError.value = null;
    if (q.length < 2) {
      candidates.value = [];
      searchingSources.value = false;
      sourceError.value = "Enter at least 2 characters.";
      return;
    }

    searchingSources.value = true;
    try {
      const { data, error } = await api.searchSourceFoods({ q, country: searchCountry, limit: 20 });
      if (
        searchId !== sourceSearchId
        || selectedFood.value?.id !== foodId
        || sourceQuery.value.trim() !== q
        || country.value !== searchCountry
      ) {
        return;
      }
      if (error || !data) {
        sourceError.value = "Unable to search nutrition sources.";
        return;
      }
      candidates.value = data;
    }
    finally {
      if (searchId === sourceSearchId) {
        searchingSources.value = false;
      }
    }
  }

  function applyFoodUpdate(
    updated: HealthLabsFoodNutritionOut,
    targetFoodId: string,
    targetCountry: string,
    startingFoodLoadId: number,
  ) {
    if (country.value !== targetCountry) {
      return;
    }
    confirmedUpdates.set(targetFoodId, { food: updated, afterLoadId: foodLoadId });
    if (foodLoadId === startingFoodLoadId) {
      foodLoadId++;
      loadingFoods.value = false;
    }
    const index = foods.value.findIndex(food => food.id === targetFoodId);
    if (index !== -1) {
      foods.value.splice(index, 1, updated);
    }
    if (selectedFood.value?.id === targetFoodId) {
      selectedFood.value = updated;
    }
  }

  async function approveNutrientFood(nutrientFoodId: string) {
    if (!selectedFood.value) {
      actionError.value = "Select a food first.";
      return false;
    }
    const targetFoodId = selectedFood.value.id;
    const targetCountry = country.value;
    const startingFoodLoadId = foodLoadId;
    saving.value = true;
    actionError.value = null;
    try {
      const { data, error } = await api.setMatch(
        targetFoodId,
        { nutrientFoodId, country: targetCountry, confidence: 1 },
        crypto.randomUUID(),
      );
      if (error || !data) {
        actionError.value = "Unable to save this source match.";
        return false;
      }
      applyFoodUpdate(data, targetFoodId, targetCountry, startingFoodLoadId);
      return true;
    }
    finally {
      saving.value = false;
    }
  }

  async function approveSource(candidate: HealthLabsNutrientFoodOut) {
    return await approveNutrientFood(candidate.id);
  }

  async function approveSuggestion() {
    const suggestion = selectedFood.value?.nutrition;
    if (!suggestion || suggestion.reviewed) {
      actionError.value = "No source suggestion is available.";
      return false;
    }
    return await approveNutrientFood(suggestion.nutrientFoodId);
  }

  async function clearMatch() {
    if (!selectedFood.value) {
      actionError.value = "Select a food first.";
      return false;
    }
    const targetFoodId = selectedFood.value.id;
    const targetCountry = country.value;
    const startingFoodLoadId = foodLoadId;
    saving.value = true;
    actionError.value = null;
    try {
      const { data, error } = await api.clearMatch(
        targetFoodId,
        targetCountry,
        crypto.randomUUID(),
      );
      if (error || !data) {
        actionError.value = "Unable to clear this source match.";
        return;
      }
      applyFoodUpdate(data, targetFoodId, targetCountry, startingFoodLoadId);
    }
    finally {
      saving.value = false;
    }
  }

  async function addPortion(payload: HealthLabsFoodPortionCreate) {
    if (!selectedFood.value) {
      actionError.value = "Select a food first.";
      return;
    }
    const targetFoodId = selectedFood.value.id;
    const targetCountry = country.value;
    const startingFoodLoadId = foodLoadId;
    saving.value = true;
    actionError.value = null;
    try {
      const { data, error } = await api.addPortion(
        targetFoodId,
        payload,
        crypto.randomUUID(),
      );
      if (error || !data) {
        actionError.value = "Unable to add this portion.";
        return false;
      }
      applyFoodUpdate(data, targetFoodId, targetCountry, startingFoodLoadId);
      return true;
    }
    finally {
      saving.value = false;
    }
  }

  return {
    foods,
    total,
    visibleFoods,
    selectedFood,
    candidates,
    workerStatus,
    reviewWork,
    country,
    queueQuery,
    sourceQuery,
    queueFilter,
    loadingFoods,
    searchingSources,
    saving,
    loadingWork,
    foodError,
    sourceError,
    actionError,
    workError,
    loadFoods,
    loadWorker,
    openWork,
    selectFood,
    searchSources,
    approveSource,
    approveSuggestion,
    clearMatch,
    addPortion,
  };
}
