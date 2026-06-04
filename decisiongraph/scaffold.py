"""OS-OWNED scaffolding — deterministic project boilerplate, by stack.

Boilerplate (package.json, tsconfig, configs, root layout) must NOT be written by
the LLM: versions and config are fixed and identical for every project of a given
stack, so an LLM only adds version-drift and errors. Instead the OS owns a
per-stack TEMPLATE REGISTRY and lays the known-good skeleton down deterministically
— exactly how `create-next-app` / `vite` scaffold real projects.

The dispatcher DECLARES the stack; the OS calls `scaffold(site_dir, stack)`.
"""
from __future__ import annotations
import os

# ── template registry: stack -> {relative_path: file_contents} ───────────────

_NEXTJS = {
    "package.json": (
        '{\n  "name": "os-app", "version": "0.1.0", "private": true,\n'
        '  "scripts": { "dev": "next dev", "build": "next build", "start": "next start" },\n'
        '  "dependencies": { "next": "14.2.5", "react": "18.3.1", "react-dom": "18.3.1" },\n'
        '  "devDependencies": { "typescript": "5.5.4", "@types/node": "20.14.10",\n'
        '    "@types/react": "18.3.3", "@types/react-dom": "18.3.0",\n'
        '    "tailwindcss": "3.4.7", "postcss": "8.4.40", "autoprefixer": "10.4.19",\n'
        '    "jest": "29.7.0", "jest-environment-jsdom": "29.7.0",\n'
        '    "@testing-library/react": "16.0.0", "@testing-library/jest-dom": "6.4.8",\n'
        '    "@types/jest": "29.5.12", "ts-node": "10.9.2" }\n}\n'),
    "next.config.js":
        "/** @type {import('next').NextConfig} */\nmodule.exports = { reactStrictMode: true };\n",
    "postcss.config.js":
        "module.exports = { plugins: { tailwindcss: {}, autoprefixer: {} } };\n",
    "tailwind.config.ts": (
        "import type { Config } from 'tailwindcss'\nconst config: Config = "
        "{ content: ['./src/**/*.{js,ts,jsx,tsx,mdx}'], theme: { extend: {} }, plugins: [] }\n"
        "export default config\n"),
    "tsconfig.json": (
        '{\n  "compilerOptions": { "target": "ES2020", "lib": ["dom","dom.iterable","esnext"],\n'
        '    "allowJs": true, "skipLibCheck": true, "strict": false, "noEmit": true,\n'
        '    "esModuleInterop": true, "module": "esnext", "moduleResolution": "bundler",\n'
        '    "resolveJsonModule": true, "isolatedModules": true, "jsx": "preserve",\n'
        '    "incremental": true, "types": ["jest", "@testing-library/jest-dom", "node"],\n'
        '    "plugins": [{ "name": "next" }], "paths": { "@/*": ["./src/*"] } },\n'
        '  "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx", ".next/types/**/*.ts"],\n'
        '  "exclude": ["node_modules"]\n}\n'),
    "jest.config.js": (
        "const nextJest = require('next/jest');\n"
        "const createJestConfig = nextJest({ dir: './' });\n"
        "module.exports = createJestConfig({ testEnvironment: 'jest-environment-jsdom',\n"
        "  setupFilesAfterEnv: ['<rootDir>/jest.setup.js'] });\n"),
    "jest.setup.js": "import '@testing-library/jest-dom';\n",
    "src/app/globals.css": "@tailwind base;\n@tailwind components;\n@tailwind utilities;\n",
    "src/app/layout.tsx": (
        "import './globals.css';\n\nexport default function RootLayout("
        "{ children }: { children: React.ReactNode }) {\n  return (<html lang=\"en\"><body>"
        "{children}</body></html>);\n}\n"),
}

_REGISTRY = {"nextjs": _NEXTJS}

# file extensions / markers that indicate a stack (for auto-detection fallback)
_DETECT = [
    ("nextjs", lambda fl: any(f.endswith((".tsx", ".jsx")) for f in fl)
                          or any("app/api/" in f for f in fl)),
]


def detect_stack(files) -> str | None:
    fl = [f.replace("\\", "/") for f in files]
    for name, test in _DETECT:
        if test(fl):
            return name
    return None


# Files the OS OWNS deterministically — always overwrite whatever an agent wrote,
# because these are pure boilerplate. An agent authoring layout.tsx tends to import
# phantom modules (providers, site-header…) that break every page.
_OWNED = {"layout.tsx", "globals.css", "package.json", "tsconfig.json",
          "next.config.js", "postcss.config.js", "tailwind.config.ts",
          "jest.config.js", "jest.setup.js"}


def scaffold(site_dir: str, stack: str, *, overwrite: bool = False) -> list[str]:
    """Lay down the deterministic skeleton for `stack`. Always overwrites the
    OS-OWNED boilerplate files (configs, layout, globals); only fills the rest if
    missing. Returns the list of files written."""
    tpl = _REGISTRY.get(stack)
    if not tpl:
        return []
    written = []
    for rel, content in tpl.items():
        p = os.path.join(site_dir, rel)
        os.makedirs(os.path.dirname(p) or site_dir, exist_ok=True)
        owned = os.path.basename(rel) in _OWNED
        if overwrite or owned or not os.path.exists(p):
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
            written.append(rel)
    return written


def ensure_homepage(site_dir: str) -> str | None:
    """Safety net: if the build produced components but no src/app/page.tsx that
    mounts them, assemble one deterministically (recursive component scan)."""
    page = os.path.join(site_dir, "src/app/page.tsx")
    comp_dir = os.path.join(site_dir, "src/components")
    if os.path.exists(page) or not os.path.isdir(comp_dir):
        return None
    import re
    comps = []
    for root, _, files in os.walk(comp_dir):
        for f in sorted(files):
            if f.endswith(".tsx"):
                rel = os.path.relpath(os.path.join(root, f), comp_dir).replace("\\", "/")[:-4]
                ident = re.sub(r"\W", "", os.path.basename(rel))
                comps.append((ident, f"@/components/{rel}"))
    if not comps:
        return None
    imports = "\n".join(f"import {i} from '{p}';" for i, p in comps)
    body = "\n      ".join(f"<{i} />" for i, _ in comps)
    with open(page, "w", encoding="utf-8") as f:
        f.write(f"{imports}\n\nexport default function Home() {{\n  return (\n"
                f"    <main>\n      {body}\n    </main>\n  );\n}}\n")
    return "src/app/page.tsx"
