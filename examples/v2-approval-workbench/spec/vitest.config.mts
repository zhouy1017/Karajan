import { fileURLToPath } from "node:url";
import { defineConfig } from "../../../frontend/node_modules/vitest/dist/config.js";
import react from "../../../frontend/node_modules/@vitejs/plugin-react/dist/index.js";

const worktree = fileURLToPath(new URL("../../..", import.meta.url));
export default defineConfig({
  root: worktree,
  plugins: [react()],
  test: {
    environment: "jsdom",
    include: ["examples/v2-approval-workbench/spec/*.test.ts"],
  },
});
