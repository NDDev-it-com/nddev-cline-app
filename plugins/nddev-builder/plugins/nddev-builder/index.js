import { readFile } from "node:fs/promises";

const REFERENCE_FILES = Object.freeze({
  "native-paths": "native-paths.md",
  skills: "skills.md",
  "rules-memory": "rules-memory.md",
  agents: "agents.md",
  plugins: "plugins.md",
  hooks: "hooks.md",
  mcp: "mcp.md",
  "profiles-runtime": "profiles-runtime.md",
  validation: "validation.md",
});

const MANAGED_FACTS = Object.freeze({
  product: "nddev-cline-app",
  setup: "nddev-builder",
  profiles: ["full-auto", "safe"],
  nativePaths: {
    config: "home/.cline/data/settings",
    rules: "home/.cline/rules",
    skills: "home/.cline/skills",
    agents: "home/.cline/agents",
    plugins: "home/.cline/plugins",
    hooks: "home/.cline/hooks",
  },
});

async function readReference(name) {
  const filename = REFERENCE_FILES[name];
  if (!filename) {
    throw new Error("unknown nddev-builder reference");
  }
  const url = new URL(`../../skills/nddev-builder/references/${filename}`, import.meta.url);
  const content = await readFile(url, { encoding: "utf-8" });
  if (Buffer.byteLength(content, "utf-8") > 65536) {
    throw new Error("nddev-builder reference exceeds the 65536-byte limit");
  }
  return content;
}

const plugin = {
  name: "nddev-builder",
  manifest: {
    capabilities: ["tools"],
  },
  setup(api) {
    if (!api || typeof api.registerTool !== "function") {
      return;
    }
    api.registerTool({
      name: "nddev_builder_facts",
      description: "Return bounded public nddev-cline-app builder facts.",
      inputSchema: {
        type: "object",
        additionalProperties: false,
        properties: {},
      },
      async execute() {
        return {
          content: [
            {
              type: "text",
              text: JSON.stringify(MANAGED_FACTS, null, 2),
            },
          ],
        };
      },
    });
    api.registerTool({
      name: "nddev_builder_read_reference",
      description: "Read one shipped nddev-builder reference file by stable id.",
      inputSchema: {
        type: "object",
        additionalProperties: false,
        required: ["name"],
        properties: {
          name: {
            type: "string",
            enum: Object.keys(REFERENCE_FILES),
          },
        },
      },
      async execute(input) {
        const text = await readReference(input?.name);
        return {
          content: [
            {
              type: "text",
              text,
            },
          ],
        };
      },
    });
  },
};

export default plugin;
