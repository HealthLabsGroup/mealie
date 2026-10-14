<template>
  <v-footer
    color="primary"
    padless
    app
  >
    <v-row
      justify="center"
      align="center"
      density="comfortable"
      no-gutters
    >
      <v-col
        class="py-2 text-center white--text"
        cols="12"
      >
        <v-btn
          data-testid="source-link"
          aria-label="Source code for this deployed version"
          color="white"
          icon
          :href="$appInfo.sourceUrl"
          target="_blank"
          rel="noopener noreferrer"
        >
          <v-icon>
            {{ $globals.icons.github }}
          </v-icon>
        </v-btn>
        {{ new Date().getFullYear() }} — <strong> Mealie </strong>
      </v-col>
    </v-row>
  </v-footer>
</template>
