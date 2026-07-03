# Admin Login Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refresh the `admin-ui` login page to a unified background layout with left-side brand copy and right-side login form while preserving the existing login behavior.

**Architecture:** Keep the existing Vue login component and submit flow intact, but replace the template structure and login-specific CSS with a lighter full-background composition. Copy the chosen image assets into `admin-ui/assets` so the static HTML app can load them locally without external dependencies.

**Tech Stack:** Static `admin-ui` Vue component modules, Element Plus, plain CSS, Node `assert`-based `.mjs` tests

---

## File Structure

- Create: `admin-ui/assets/login-bg.png`
- Create: `admin-ui/assets/company-logo.png`
- Create: `admin-ui/login_redesign.test.mjs`
- Modify: `admin-ui/src/views/Login.js`
- Modify: `admin-ui/style.css`

### Task 1: Add a failing login redesign test

**Files:**
- Create: `admin-ui/login_redesign.test.mjs`
- Test: `admin-ui/login_redesign.test.mjs`

- [ ] **Step 1: Write the failing test**

```javascript
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const loginJs = readFileSync(new URL('./src/views/Login.js', import.meta.url), 'utf-8');
const styleCss = readFileSync(new URL('./style.css', import.meta.url), 'utf-8');

assert.ok(loginJs.includes('login-shell'), 'expected unified login shell markup');
assert.ok(loginJs.includes('company-logo.png'), 'expected local company logo asset reference');
assert.ok(loginJs.includes('login-hero'), 'expected left-side hero section');
assert.ok(styleCss.includes('.login-shell'), 'expected login shell styles');
assert.ok(styleCss.includes('.login-bg-layer'), 'expected unified background layer styles');
assert.ok(styleCss.includes('.login-panel'), 'expected floating login card styles');

console.log('login redesign test passed');
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node admin-ui/login_redesign.test.mjs`
Expected: FAIL because `login-shell` and related selectors do not exist yet

### Task 2: Add local static assets for the redesigned login page

**Files:**
- Create: `admin-ui/assets/login-bg.png`
- Create: `admin-ui/assets/company-logo.png`

- [ ] **Step 1: Copy the approved source assets into local static paths**

Use local copies of the chosen background image and the provided company logo so the static `admin-ui` app can reference them with stable relative URLs.

- [ ] **Step 2: Verify the assets exist**

Run: `Get-ChildItem admin-ui/assets`
Expected: both `login-bg.png` and `company-logo.png` are present

### Task 3: Update the login component structure and copy

**Files:**
- Modify: `admin-ui/src/views/Login.js`
- Test: `admin-ui/login_redesign.test.mjs`

- [ ] **Step 1: Replace the login template structure**

Update the template so it contains:

- a unified `login-shell`
- a full-page `login-bg-layer`
- a left `login-hero` area with current product title, subtitle, and 2-3 capability bullets
- a right `login-panel` area with the existing title, email field, password field, and login button only
- a local company logo image reference

- [ ] **Step 2: Keep submit behavior unchanged**

Preserve the current `submit()` function logic, redirect behavior, and preview fallback behavior while only refining visible copy where needed.

- [ ] **Step 3: Run the redesign test**

Run: `node admin-ui/login_redesign.test.mjs`
Expected: PASS

### Task 4: Replace login-specific CSS with the new unified layout

**Files:**
- Modify: `admin-ui/style.css`
- Test: `admin-ui/login_redesign.test.mjs`

- [ ] **Step 1: Replace the login page styles**

Add styles for:

- `.login-shell`
- `.login-bg-layer`
- `.login-content`
- `.login-hero`
- `.login-panel`
- responsive single-column behavior

Ensure the page uses one full-screen background image with a light overlay and two floating content zones instead of a hard split layout.

- [ ] **Step 2: Keep the rest of the admin styles untouched**

Limit changes to the login-related selector block so ongoing work in other pages is not disturbed.

- [ ] **Step 3: Run the redesign test again**

Run: `node admin-ui/login_redesign.test.mjs`
Expected: PASS

### Task 5: Run broader verification

**Files:**
- Test: `admin-ui/login_redesign.test.mjs`
- Test: `admin-ui/preview_mode.test.mjs`
- Test: `admin-ui/v016.test.mjs`

- [ ] **Step 1: Run targeted login redesign verification**

Run: `node admin-ui/login_redesign.test.mjs`
Expected: PASS

- [ ] **Step 2: Run preview-mode regression check**

Run: `node admin-ui/preview_mode.test.mjs`
Expected: `5 preview mode tests passed`

- [ ] **Step 3: Run shared admin-ui regression check**

Run: `node admin-ui/v016.test.mjs`
Expected: existing assertions remain green

## Self-Review

- Spec coverage: the plan covers unified background layout, left brand text, right login-only form, auxiliary company logo, and unchanged login behavior.
- Placeholder scan: no `TODO` or deferred implementation markers remain.
- Consistency: asset names, class names, and file paths are consistent across tasks.

Plan complete and saved to `docs/superpowers/plans/2026-06-30-admin-login-redesign.md`.
User preference already chose inline execution in this session, so implementation may proceed here without additional git steps.
