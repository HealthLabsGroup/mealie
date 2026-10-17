<template>
  <v-container
    fluid
    class="nutrition-curator pa-4 pa-md-8"
  >
    <div class="d-flex flex-column flex-lg-row align-lg-end justify-space-between ga-4 mb-6">
      <div>
        <h1 class="text-h4 font-weight-bold mb-1">
          {{ $t("nutrition-curator.title") }}
        </h1>
        <p class="text-body-1 text-medium-emphasis mb-0">
          {{ $t("nutrition-curator.subtitle") }}
        </p>
      </div>

      <div class="d-flex flex-column flex-sm-row ga-3 curator-toolbar">
        <v-select
          v-model="country"
          :items="countries"
          :label="$t('nutrition-curator.country')"
          density="comfortable"
          variant="outlined"
          hide-details
          @update:model-value="refreshCountry"
        />
        <v-form
          class="d-flex ga-2 flex-grow-1"
          @submit.prevent="loadFoods"
        >
          <v-text-field
            v-model="queueQuery"
            :label="$t('nutrition-curator.search-foods')"
            density="comfortable"
            variant="outlined"
            clearable
            hide-details
          />
          <v-btn
            type="submit"
            color="primary"
            size="large"
            :loading="loadingFoods"
          >
            {{ $t("nutrition-curator.search") }}
          </v-btn>
        </v-form>
      </div>
    </div>

    <v-card
      v-if="workerStatus || reviewWork.length || workError || loadingWork"
      data-testid="worker-status"
      variant="outlined"
      class="mb-6"
    >
      <v-card-title class="d-flex flex-wrap align-center justify-space-between ga-3">
        <span>{{ $t("nutrition-curator.background-title") }}</span>
        <v-btn
          variant="text"
          size="small"
          :loading="loadingWork"
          @click="refreshAll"
        >
          {{ $t("nutrition-curator.refresh") }}
        </v-btn>
      </v-card-title>
      <v-card-text>
        <div
          v-if="workerStatus"
          class="d-flex flex-wrap ga-2 mb-3"
        >
          <v-chip
            v-for="state in workerStates"
            :key="state"
            size="small"
            variant="tonal"
          >
            {{ workerStateLabel(state) }}: {{ workerStatus.counts[state] ?? 0 }}
          </v-chip>
        </div>
        <v-alert
          v-if="workerStatus && !workerStatus.providerEnabled"
          type="info"
          variant="tonal"
          class="mb-3"
        >
          {{ $t("nutrition-curator.provider-disabled") }}
        </v-alert>
        <v-alert
          v-else-if="workerStatus?.providerBudgetExhausted"
          type="warning"
          variant="tonal"
          class="mb-3"
        >
          {{ $t("nutrition-curator.provider-budget-exhausted") }}
        </v-alert>
        <v-alert
          v-if="workError"
          type="error"
          variant="tonal"
          class="mb-3"
          aria-live="polite"
        >
          {{ workError }}
        </v-alert>

        <v-list
          v-if="reviewWork.length"
          lines="two"
          class="pa-0"
        >
          <v-list-item
            v-for="work in reviewWork"
            :key="work.id"
          >
            <v-list-item-title>{{ workTitle(work) }}</v-list-item-title>
            <v-list-item-subtitle>{{ workSubtitle(work) }}</v-list-item-subtitle>
            <template #append>
              <v-btn
                v-if="work.kind !== 'ingredient_structure'"
                color="primary"
                size="small"
                variant="outlined"
                :data-testid="work.kind === 'portion_resolution' ? 'open-portion-work' : 'open-food-work'"
                @click="openWork(work)"
              >
                {{ work.kind === "portion_resolution"
                  ? $t("nutrition-curator.add-portion")
                  : $t("nutrition-curator.review-food") }}
              </v-btn>
            </template>
          </v-list-item>
        </v-list>
        <p
          v-else-if="!loadingWork"
          class="text-body-2 text-medium-emphasis mb-0"
        >
          {{ $t("nutrition-curator.no-background-work") }}
        </p>
      </v-card-text>
    </v-card>

    <v-row>
      <v-col
        cols="12"
        md="4"
        lg="3"
      >
        <v-card
          variant="outlined"
          class="h-100"
        >
          <v-card-title class="d-flex align-center justify-space-between">
            <span>{{ $t("nutrition-curator.food-queue") }}</span>
            <v-chip
              size="small"
              variant="tonal"
            >
              {{ total }}
            </v-chip>
          </v-card-title>
          <v-card-text class="pb-2">
            <v-btn-toggle
              v-model="queueFilter"
              mandatory
              divided
              variant="outlined"
              class="w-100"
              :aria-label="$t('nutrition-curator.queue-filter')"
            >
              <v-btn
                value="needs-review"
                size="small"
                class="flex-grow-1"
                data-testid="filter-needs-review"
              >
                {{ $t("nutrition-curator.needs-review") }}
              </v-btn>
              <v-btn
                value="matched"
                size="small"
                class="flex-grow-1"
                data-testid="filter-matched"
              >
                {{ $t("nutrition-curator.matched") }}
              </v-btn>
              <v-btn
                value="all"
                size="small"
                class="flex-grow-1"
                data-testid="filter-all"
              >
                {{ $t("nutrition-curator.all") }}
              </v-btn>
            </v-btn-toggle>
          </v-card-text>

          <v-progress-linear
            v-if="loadingFoods"
            indeterminate
            color="primary"
          />
          <v-alert
            v-if="foodError"
            type="error"
            variant="tonal"
            class="ma-4 mt-2"
            aria-live="polite"
          >
            {{ foodError }}
          </v-alert>

          <v-list
            v-if="visibleFoods.length"
            lines="two"
            class="py-0 curator-food-list"
          >
            <v-list-item
              v-for="food in visibleFoods"
              :key="food.id"
              :active="selectedFood?.id === food.id"
              color="primary"
              @click="selectFood(food)"
            >
              <v-list-item-title>{{ food.name || $t("nutrition-curator.unnamed-food") }}</v-list-item-title>
              <v-list-item-subtitle>{{ statusLabel(food.status) }}</v-list-item-subtitle>
            </v-list-item>
          </v-list>
          <p
            v-else-if="!loadingFoods && !foodError"
            class="text-body-2 text-medium-emphasis pa-4"
            aria-live="polite"
          >
            {{ $t("nutrition-curator.no-foods") }}
          </p>
        </v-card>
      </v-col>

      <v-col
        cols="12"
        md="8"
        lg="9"
      >
        <v-card
          v-if="!selectedFood"
          variant="outlined"
          class="pa-8 text-center text-medium-emphasis"
        >
          {{ $t("nutrition-curator.select-food") }}
        </v-card>

        <div v-else>
          <v-card
            variant="outlined"
            class="mb-4"
          >
            <v-card-title class="d-flex flex-wrap align-center ga-3">
              <span>{{ selectedFood.name || $t("nutrition-curator.unnamed-food") }}</span>
              <v-chip
                size="small"
                variant="tonal"
                :color="selectedFood.status === 'matched' ? 'success' : 'warning'"
              >
                {{ statusLabel(selectedFood.status) }}
              </v-chip>
            </v-card-title>

            <template v-if="hasReviewedMatch && selectedFood.nutrition">
              <v-divider />
              <v-card-text>
                <div class="d-flex flex-column flex-sm-row justify-space-between ga-3 mb-4">
                  <div>
                    <h2 class="text-subtitle-1 font-weight-bold mb-1">
                      {{ $t("nutrition-curator.current-source") }}
                    </h2>
                    <p class="text-body-2 text-medium-emphasis mb-0">
                      {{ sourceLine(selectedFood.nutrition.source) }}
                    </p>
                    <p class="text-body-1 mb-0">
                      {{ selectedFood.nutrition.name }}
                    </p>
                    <p class="text-caption text-medium-emphasis mb-1">
                      {{ $t("nutrition-curator.source-id") }}: {{ selectedFood.nutrition.sourceFoodId }}
                    </p>
                    <a
                      :href="selectedFood.nutrition.source.sourceUrl"
                      target="_blank"
                      rel="noopener noreferrer"
                      class="text-caption mr-3"
                    >{{ $t("nutrition-curator.source-data") }}</a>
                    <a
                      :href="selectedFood.nutrition.source.licenseUrl"
                      target="_blank"
                      rel="noopener noreferrer"
                      class="text-caption"
                    >{{ $t("nutrition-curator.license") }}</a>
                  </div>
                  <v-btn
                    variant="outlined"
                    color="error"
                    :loading="saving"
                    data-testid="clear-match"
                    @click="clearMatch"
                  >
                    {{ $t("nutrition-curator.clear-match") }}
                  </v-btn>
                </div>

                <dl class="nutrient-grid">
                  <div
                    v-for="nutrient in nutrientFields"
                    :key="nutrient.key"
                    class="nutrient-value"
                  >
                    <dt>{{ nutrient.label }}</dt>
                    <dd>{{ formatNutrient(selectedFood.nutrition.nutrientsPer100g[nutrient.key], nutrient.unit) }}</dd>
                  </div>
                </dl>
              </v-card-text>
            </template>
            <template v-else-if="selectedFood.nutrition">
              <v-divider />
              <v-card-text>
                <div class="d-flex flex-column flex-sm-row justify-space-between ga-3 mb-4">
                  <div>
                    <h2 class="text-subtitle-1 font-weight-bold mb-1">
                      {{ $t("nutrition-curator.suggested-source") }}
                    </h2>
                    <p class="text-body-2 text-medium-emphasis mb-0">
                      {{ sourceLine(selectedFood.nutrition.source) }}
                    </p>
                    <p class="text-body-1 mb-0">
                      {{ selectedFood.nutrition.name }}
                    </p>
                    <p class="text-caption text-medium-emphasis mb-0">
                      {{ $t("nutrition-curator.suggestion-help") }}
                    </p>
                  </div>
                  <v-btn
                    color="primary"
                    :loading="saving"
                    data-testid="use-suggestion"
                    @click="approveSuggestion"
                  >
                    {{ $t("nutrition-curator.use-suggestion") }}
                  </v-btn>
                </div>

                <dl class="nutrient-grid">
                  <div
                    v-for="nutrient in nutrientFields"
                    :key="nutrient.key"
                    class="nutrient-value"
                  >
                    <dt>{{ nutrient.label }}</dt>
                    <dd>{{ formatNutrient(selectedFood.nutrition.nutrientsPer100g[nutrient.key], nutrient.unit) }}</dd>
                  </div>
                </dl>
              </v-card-text>
            </template>
          </v-card>

          <v-card
            variant="outlined"
            class="mb-4"
          >
            <v-card-title>{{ $t("nutrition-curator.find-source") }}</v-card-title>
            <v-card-text>
              <v-form
                class="d-flex flex-column flex-sm-row ga-2 mb-4"
                @submit.prevent="searchSources"
              >
                <v-text-field
                  v-model="sourceQuery"
                  :label="$t('nutrition-curator.source-search')"
                  variant="outlined"
                  density="comfortable"
                  hide-details
                />
                <v-btn
                  type="submit"
                  color="primary"
                  size="large"
                  :loading="searchingSources"
                >
                  {{ $t("nutrition-curator.find") }}
                </v-btn>
              </v-form>

              <v-alert
                v-if="sourceError"
                type="error"
                variant="tonal"
                class="mb-4"
                aria-live="polite"
              >
                {{ sourceError }}
              </v-alert>

              <div class="d-flex flex-column ga-3">
                <v-card
                  v-for="candidate in candidates"
                  :key="candidate.id"
                  variant="tonal"
                >
                  <v-card-text>
                    <div class="d-flex flex-column flex-md-row justify-space-between ga-3 mb-4">
                      <div>
                        <p class="text-caption text-medium-emphasis mb-1">
                          {{ sourceLine(candidate.source) }}
                        </p>
                        <h3 class="text-h6">
                          {{ candidate.name }}
                        </h3>
                        <p
                          v-if="candidate.preparationState"
                          class="text-body-2 text-medium-emphasis mb-0"
                        >
                          {{ candidate.preparationState }}
                        </p>
                        <p class="text-caption text-medium-emphasis mb-1">
                          {{ $t("nutrition-curator.source-id") }}: {{ candidate.sourceFoodId }}
                        </p>
                        <a
                          :href="candidate.source.sourceUrl"
                          target="_blank"
                          rel="noopener noreferrer"
                          class="text-caption mr-3"
                        >{{ $t("nutrition-curator.source-data") }}</a>
                        <a
                          :href="candidate.source.licenseUrl"
                          target="_blank"
                          rel="noopener noreferrer"
                          class="text-caption"
                        >{{ $t("nutrition-curator.license") }}</a>
                      </div>
                      <v-btn
                        color="primary"
                        :loading="saving"
                        data-testid="use-source"
                        @click="approveSource(candidate)"
                      >
                        {{ $t("nutrition-curator.use-source") }}
                      </v-btn>
                    </div>

                    <dl class="nutrient-grid">
                      <div
                        v-for="nutrient in nutrientFields"
                        :key="nutrient.key"
                        class="nutrient-value"
                        :data-testid="`candidate-nutrient-${nutrient.id}`"
                      >
                        <dt>{{ nutrient.label }}</dt>
                        <dd>{{ formatNutrient(candidate.nutrientsPer100g[nutrient.key], nutrient.unit) }}</dd>
                      </div>
                    </dl>

                    <p class="text-caption text-medium-emphasis mt-3 mb-0">
                      {{ $t("nutrition-curator.per-100g") }} · {{ candidate.source.attribution }}
                    </p>
                    <p class="text-caption text-medium-emphasis mb-0">
                      SHA-256: {{ candidate.source.sha256 }}
                    </p>
                  </v-card-text>
                </v-card>
              </div>

              <p
                v-if="!searchingSources && !sourceError && sourceQuery.trim().length >= 2 && !candidates.length"
                class="text-body-2 text-medium-emphasis mb-0"
                aria-live="polite"
              >
                {{ $t("nutrition-curator.no-candidates") }}
              </p>
            </v-card-text>
          </v-card>

          <v-card
            v-if="hasReviewedMatch && selectedFood.nutrition"
            variant="outlined"
          >
            <v-card-title>{{ $t("nutrition-curator.measured-portions") }}</v-card-title>
            <v-card-text>
              <v-chip
                v-for="portionItem in selectedFood.nutrition.portions"
                :key="`${portionItem.description}-${portionItem.amount}-${portionItem.unit}`"
                class="mr-2 mb-3"
                variant="outlined"
              >
                {{ portionItem.description }}: {{ portionItem.amount }} {{ portionItem.unit }} = {{ portionItem.grams }} g
              </v-chip>
              <p
                v-if="!selectedFood.nutrition.portions?.length"
                class="text-body-2 text-medium-emphasis"
              >
                {{ $t("nutrition-curator.no-portions") }}
              </p>

              <v-form
                class="portion-form"
                data-testid="portion-form"
                @submit.prevent="submitPortion"
              >
                <v-text-field
                  v-model="portion.description"
                  :label="$t('nutrition-curator.description')"
                  data-testid="portion-description"
                  variant="outlined"
                  density="comfortable"
                />
                <v-text-field
                  v-model.number="portion.amount"
                  :label="$t('nutrition-curator.amount')"
                  type="number"
                  min="0.01"
                  step="any"
                  data-testid="portion-amount"
                  variant="outlined"
                  density="comfortable"
                />
                <v-text-field
                  v-model="portion.unit"
                  :label="$t('nutrition-curator.unit')"
                  data-testid="portion-unit"
                  variant="outlined"
                  density="comfortable"
                />
                <v-text-field
                  v-model.number="portion.grams"
                  :label="$t('nutrition-curator.grams')"
                  type="number"
                  min="0.01"
                  step="any"
                  data-testid="portion-grams"
                  variant="outlined"
                  density="comfortable"
                />
                <v-btn
                  type="submit"
                  color="primary"
                  size="large"
                  :disabled="!portionValid"
                  :loading="saving"
                  data-testid="add-portion"
                >
                  {{ $t("nutrition-curator.add-portion") }}
                </v-btn>
              </v-form>
            </v-card-text>
          </v-card>

          <v-alert
            v-if="actionError"
            type="error"
            variant="tonal"
            class="mt-4"
            aria-live="polite"
          >
            {{ actionError }}
          </v-alert>
        </div>
      </v-col>
    </v-row>
  </v-container>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, watch } from "vue";
import { useNutritionCurator } from "~/composables/use-nutrition-curator";
import type {
  HealthLabsFoodPortionCreate,
  HealthLabsNutritionSourceOut,
  HealthLabsNutritionStatus,
  HealthLabsNutritionValues,
  HealthLabsNutritionWorkOut,
} from "~/lib/api/types/healthlabs";

definePageMeta({ middleware: ["auth", "nutrition-write-only"] });

const i18n = useI18n();
const {
  total,
  visibleFoods,
  selectedFood,
  candidates,
  country,
  queueQuery,
  sourceQuery,
  queueFilter,
  loadingFoods,
  searchingSources,
  saving,
  foodError,
  sourceError,
  actionError,
  workerStatus,
  reviewWork,
  loadingWork,
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
} = useNutritionCurator();

const countries = ["GB", "FR", "DE", "US", "OTHER"];
const workerStates = ["pending", "running", "review_required", "completed", "failed"] as const;
const nutrientFields: ReadonlyArray<{
  id: string;
  key: keyof HealthLabsNutritionValues;
  label: string;
  unit: string;
}> = [
  { id: "energy", key: "kcal", label: i18n.t("nutrition-curator.energy"), unit: "kcal" },
  { id: "protein", key: "proteinG", label: i18n.t("nutrition-curator.protein"), unit: "g" },
  { id: "fibre", key: "fibreG", label: i18n.t("nutrition-curator.fibre"), unit: "g" },
  { id: "carbs", key: "carbsG", label: i18n.t("nutrition-curator.carbs"), unit: "g" },
  { id: "fat", key: "fatG", label: i18n.t("nutrition-curator.fat"), unit: "g" },
];

const portion = reactive<HealthLabsFoodPortionCreate>({
  description: "",
  amount: 1,
  unit: "",
  grams: 0,
});
const hasReviewedMatch = computed(() => (
  selectedFood.value?.status === "matched" && selectedFood.value.nutrition?.reviewed === true
));
const portionValid = computed(() => (
  portion.description.trim().length > 0
  && portion.amount > 0
  && portion.unit.trim().length > 0
  && portion.grams > 0
));

function resetPortion() {
  portion.description = "";
  portion.amount = 1;
  portion.unit = "";
  portion.grams = 0;
}

watch(
  [
    () => selectedFood.value?.id,
    () => selectedFood.value?.nutrition?.nutrientFoodId,
    () => selectedFood.value?.nutrition?.reviewed,
  ],
  resetPortion,
);

function statusLabel(status: HealthLabsNutritionStatus) {
  return i18n.t(`nutrition-curator.status-${status.replace("_", "-")}`);
}

function sourceLine(source: HealthLabsNutritionSourceOut) {
  return `${source.provider} · ${source.country} · ${source.version}`;
}

function formatNutrient(value: number | null | undefined, unit: string) {
  return value == null ? "—" : `${value} ${unit}`;
}

function workerStateLabel(value: string) {
  return i18n.t(`nutrition-curator.worker-${value.replaceAll("_", "-")}`);
}

function workTitle(work: HealthLabsNutritionWorkOut) {
  if (work.kind !== "ingredient_structure") {
    return work.kind === "portion_resolution"
      ? i18n.t("nutrition-curator.portion-work")
      : i18n.t("nutrition-curator.food-work");
  }
  const suggestion = work.result?.suggestion;
  if (!suggestion || typeof suggestion !== "object" || Array.isArray(suggestion)) {
    return i18n.t("nutrition-curator.ingredient-work");
  }
  const values = suggestion as Record<string, unknown>;
  return [values.quantity, values.unit, values.food].filter(value => value != null).join(" ");
}

function workSubtitle(work: HealthLabsNutritionWorkOut) {
  if (work.kind !== "ingredient_structure") {
    return workerStateLabel(work.kind);
  }
  const reason = work.result?.reason;
  if (reason === "provider_disabled") {
    return i18n.t("nutrition-curator.ingredient-provider-disabled");
  }
  if (reason === "provider_unconfigured") {
    return i18n.t("nutrition-curator.ingredient-provider-unconfigured");
  }
  return i18n.t("nutrition-curator.ingredient-suggestion-unresolved");
}

async function refreshAll() {
  await Promise.all([loadFoods(), loadWorker()]);
}

async function refreshCountry() {
  const loaded = await loadFoods();
  if (loaded && selectedFood.value) {
    await searchSources();
  }
}

async function submitPortion() {
  if (!portionValid.value) {
    return;
  }
  const saved = await addPortion({
    description: portion.description.trim(),
    amount: portion.amount,
    unit: portion.unit.trim(),
    grams: portion.grams,
  });
  if (saved) {
    resetPortion();
  }
}

onMounted(refreshAll);
</script>

<style scoped>
.nutrition-curator {
  max-width: 1600px;
}

.curator-toolbar {
  width: min(100%, 700px);
}

.curator-toolbar > :first-child {
  flex: 0 0 150px;
}

.curator-food-list {
  max-height: 65vh;
  overflow-y: auto;
}

.nutrient-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(80px, 1fr));
  gap: 1px;
  overflow-x: auto;
}

.nutrient-value {
  min-width: 80px;
  padding: 12px;
  background: rgb(var(--v-theme-surface-variant));
}

.nutrient-value dt {
  color: rgb(var(--v-theme-on-surface-variant));
  font-size: 0.75rem;
}

.nutrient-value dd {
  margin: 4px 0 0;
  font-size: 1rem;
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}

.portion-form {
  display: grid;
  grid-template-columns: 2fr repeat(3, minmax(110px, 1fr)) auto;
  gap: 12px;
  align-items: start;
}

@media (max-width: 959px) {
  .curator-food-list {
    max-height: 320px;
  }

  .portion-form {
    grid-template-columns: 1fr 1fr;
  }
}

@media (max-width: 599px) {
  .curator-toolbar > :first-child {
    flex-basis: auto;
  }

  .portion-form {
    grid-template-columns: 1fr;
  }
}
</style>
