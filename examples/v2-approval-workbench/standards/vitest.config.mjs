import { defineConfig } from "../../../frontend/node_modules/vitest/dist/config.js";
import react from "../../../frontend/node_modules/@vitejs/plugin-react/dist/index.js";
import { fileURLToPath } from "node:url";
const packages = fileURLToPath(
  new URL("../../../frontend/node_modules/", import.meta.url),
);
export default defineConfig({
  root: process.cwd(),
  cacheDir: ".cache/v2-ui-standards/vite-cache",
  plugins: [react()],
  resolve: {
    alias: {
      react: packages + "react",
      "react-dom": packages + "react-dom",
      "@testing-library/react": packages + "@testing-library/react",
      "@testing-library/user-event": packages + "@testing-library/user-event",
      vitest: packages + "vitest",
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    include: ["examples/v2-approval-workbench/standards/independent.test.tsx"],
  },
});
