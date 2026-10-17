import { useHealthLabsCapabilities } from "~/composables/use-healthlabs-capabilities";

export default defineNuxtRouteMiddleware(async () => {
  const { canWriteNutrition, load } = useHealthLabsCapabilities();
  await load(true);
  if (!canWriteNutrition.value) {
    return navigateTo("/");
  }
});
