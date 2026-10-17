import { resetGroupRecipeActions } from "~/composables/use-group-recipe-actions";
import { resetGroupSelf } from "~/composables/use-groups";
import { resetHouseholdSelf } from "~/composables/use-households";
import { resetUserSelfRatings } from "~/composables/use-users/user-ratings";
import { resetBackups } from "~/composables/use-backups";
import { resetRecipes } from "~/composables/recipes/use-recipes";
import { resetUserRegistrationForm } from "~/composables/use-users/user-registration-form";
import { resetHealthLabsCapabilities } from "~/composables/use-healthlabs-capabilities";

export function clearComposableCaches() {
  resetGroupRecipeActions();
  resetGroupSelf();
  resetHouseholdSelf();
  resetUserSelfRatings();
  resetBackups();
  resetRecipes();
  resetUserRegistrationForm();
  resetHealthLabsCapabilities();
}
