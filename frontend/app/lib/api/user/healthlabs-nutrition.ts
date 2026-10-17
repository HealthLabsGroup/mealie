import { BaseAPI } from "../base/base-clients";
import type { HealthLabsCapability } from "~/lib/api/types/healthlabs";

const baseRoute = "/api/healthlabs/v1";

export class HealthLabsNutritionApi extends BaseAPI {
  async getCapabilities() {
    return await this.requests.get<HealthLabsCapability[]>(`${baseRoute}/capabilities`);
  }
}
