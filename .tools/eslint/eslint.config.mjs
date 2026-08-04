import js from "@eslint/js";
import tseslint from "typescript-eslint";
import security from "eslint-plugin-security";

export default [
  js.configs.recommended,
  ...tseslint.configs.recommended,
  security.configs.recommended,
  {
    languageOptions: { globals: { console: "readonly", process: "readonly",
      require: "readonly", module: "readonly", window: "readonly",
      document: "readonly", setTimeout: "readonly", fetch: "readonly" } },
    rules: { "@typescript-eslint/no-explicit-any": "off" },
  },
];
