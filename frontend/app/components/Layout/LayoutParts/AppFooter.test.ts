import { mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { defineComponent } from "vue";
import { createVuetify } from "vuetify";
import { VApp, VBtn, VCol, VFooter, VIcon, VRow } from "vuetify/components";
import AppFooter from "./AppFooter.vue";

const FooterHost = defineComponent({
  components: { AppFooter },
  template: "<v-app><AppFooter /></v-app>",
});

describe("AppFooter", () => {
  beforeEach(() => {
    vi.stubGlobal("ResizeObserver", class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
  });

  afterEach(() => vi.unstubAllGlobals());

  test("links users to the exact public source supplied by the server", () => {
    const sourceUrl = "https://github.com/HealthLabsGroup/mealie/tree/0123456789abcdef0123456789abcdef01234567";
    const wrapper = mount(FooterHost, {
      global: {
        plugins: [createVuetify({ components: { VApp, VBtn, VCol, VFooter, VIcon, VRow } })],
        mocks: {
          $appInfo: { sourceUrl },
          $globals: { icons: { github: "github" } },
        },
      },
    });

    const link = wrapper.get("[data-testid=\"source-link\"]");
    expect(link.attributes("href")).toBe(sourceUrl);
    expect(link.attributes("aria-label")).toBe("Source code for this deployed version");
  });
});
