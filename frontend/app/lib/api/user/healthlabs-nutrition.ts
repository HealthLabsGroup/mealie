import { BaseAPI } from "../base/base-clients";
import { route } from "~/lib/api/base/route";
import type {
  HealthLabsCapability,
  HealthLabsFoodMatchWrite,
  HealthLabsFoodNutritionOut,
  HealthLabsFoodNutritionPage,
  HealthLabsFoodPortionCreate,
  HealthLabsNutrientFoodOut,
  HealthLabsNutritionStatus,
  HealthLabsNutritionWorkerStatus,
  HealthLabsNutritionWorkKind,
  HealthLabsNutritionWorkOut,
  HealthLabsNutritionWorkState,
} from "~/lib/api/types/healthlabs";

const baseRoute = "/api/healthlabs/v1";
const nutritionRoute = `${baseRoute}/nutrition`;

function writeConfig(idempotencyKey: string, accessReason: string) {
  return {
    headers: {
      "Idempotency-Key": idempotencyKey,
      "X-HealthLabs-Access-Reason": accessReason,
    },
  };
}

export class HealthLabsNutritionApi extends BaseAPI {
  async getCapabilities() {
    return await this.requests.get<HealthLabsCapability[]>(`${baseRoute}/capabilities`);
  }

  async getFoods(params: { q?: string; country: string; status?: HealthLabsNutritionStatus }) {
    return await this.requests.get<HealthLabsFoodNutritionPage>(
      route(`${nutritionRoute}/foods`, {
        ...(params.q ? { q: params.q } : {}),
        country: params.country,
        ...(params.status ? { status: params.status } : {}),
      }),
    );
  }

  async searchSourceFoods(params: { q: string; country: string; limit?: number }) {
    return await this.requests.get<HealthLabsNutrientFoodOut[]>(
      route(`${nutritionRoute}/source-foods`, {
        q: params.q,
        country: params.country,
        ...(params.limit ? { limit: params.limit } : {}),
      }),
    );
  }

  async setMatch(foodId: string, payload: HealthLabsFoodMatchWrite, idempotencyKey: string) {
    return await this.requests.put<HealthLabsFoodNutritionOut, HealthLabsFoodMatchWrite>(
      `${nutritionRoute}/foods/${foodId}/match`,
      payload,
      writeConfig(idempotencyKey, "Nutrition curator source review"),
    );
  }

  async clearMatch(foodId: string, country: string, idempotencyKey: string) {
    return await this.requests.delete<HealthLabsFoodNutritionOut>(
      route(`${nutritionRoute}/foods/${foodId}/match`, { country }),
      writeConfig(idempotencyKey, "Nutrition curator match removal"),
    );
  }

  async addPortion(foodId: string, payload: HealthLabsFoodPortionCreate, idempotencyKey: string) {
    return await this.requests.post<HealthLabsFoodNutritionOut>(
      `${nutritionRoute}/foods/${foodId}/portions`,
      payload,
      writeConfig(idempotencyKey, "Nutrition curator measured portion"),
    );
  }

  async getWorkerStatus() {
    return await this.requests.get<HealthLabsNutritionWorkerStatus>(`${nutritionRoute}/worker-status`);
  }

  async getWork(params: { state: HealthLabsNutritionWorkState; kind?: HealthLabsNutritionWorkKind }) {
    return await this.requests.get<HealthLabsNutritionWorkOut[]>(
      route(`${nutritionRoute}/work`, {
        state: params.state,
        ...(params.kind ? { kind: params.kind } : {}),
      }),
    );
  }

  async approveWork(workId: string, idempotencyKey: string) {
    return await this.requests.post<HealthLabsNutritionWorkOut>(
      `${nutritionRoute}/work/${workId}/approve`,
      {},
      writeConfig(idempotencyKey, "Nutrition curator ingredient review"),
    );
  }
}
