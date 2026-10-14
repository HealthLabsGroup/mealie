import { mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { defineComponent } from "vue";
import { createVuetify } from "vuetify";
import { VApp, VBanner, VBtn, VCol, VFooter, VIcon, VMain, VRow } from "vuetify/components";
import BlankLayout from "./blank.vue";
import AppFooter from "~/components/Layout/LayoutParts/AppFooter.vue";

vi.mock("~/components/Layout/LayoutParts/TheSnackbar.vue", () => ({
  default: { template: "<div />" },
}));

const Passthrough = defineComponent({ template: "<div><slot /></div>" });

describe("blank layout", () => {
  beforeEach(() => {
    vi.stubGlobal("ResizeObserver", class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
  });

  afterEach(() => vi.unstubAllGlobals());

  test("shows the exact source link to logged-out users", () => {
    const sourceUrl = "https://github.com/HealthLabsGroup/mealie/tree/0123456789abcdef0123456789abcdef01234567";
    const wrapper = mount(BlankLayout, {
      global: {
        components: { AppFooter },
        plugins: [createVuetify({ components: { VApp, VBanner, VBtn, VCol, VFooter, VIcon, VMain, VRow } })],
        mocks: {
          $appInfo: { demoStatus: false, sourceUrl },
          $globals: { icons: { github: "github" } },
        },
        stubs: {
          NuxtPage: true,
          TheSnackbar: true,
          VScrollXTransition: Passthrough,
        },
      },
    });

    expect(wrapper.get("[data-testid=\"source-link\"]").attributes("href")).toBe(sourceUrl);
  });
});
