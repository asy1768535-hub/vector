# Admin UI Login Page Redesign

## Summary

Redesign the `admin-ui` login page so it feels closer to the provided reference image while keeping the current login behavior unchanged. The page should use a single full-page background image to visually connect the left brand copy area and the right login form area, avoiding a hard split-screen look.

## Goals

- Refresh the `admin-ui` login page to a lighter, more polished enterprise style.
- Keep the current product title on the page as the primary brand name.
- Add the provided company logo image as a secondary brand mark near the left-side heading.
- Use one shared background image across the full page so the left and right sections feel connected.
- Preserve the existing login flow, API calls, route redirects, and button count.
- Keep the page responsive, with a clean single-column layout on smaller screens.

## Non-Goals

- No new buttons, links, checkboxes, or backend-dependent interactions.
- No changes to authentication APIs, store behavior, route guards, or redirect logic.
- No redesign of other `admin-ui` pages in this task.
- No requirement to match the reference image pixel-for-pixel.

## Constraints

- Only the login page visual structure, copy, and styling may change.
- The right-side form must keep the existing fields: email, password, and login button.
- Existing submit behavior and preview-mode fallback behavior must remain intact.
- If the current reference background asset is too dark or visually noisy, it may be replaced with a better-fit image, including a generated image if needed.

## User Experience Design

### Overall layout

Use a full-screen background image that spans the entire page. Place two floating content zones on top of it:

- A left-aligned brand narrative area with title, short description, and a small set of capability bullets.
- A right-aligned login card with the existing form controls.

The two zones should feel part of the same scene rather than separate panels. Separation should come from spacing, translucent surfaces, subtle borders, and soft shadows instead of hard vertical slicing.

### Left brand area

The left side should include:

- The main title: the current product title already used by the login page
- A short subtitle describing the product as an internal knowledge management and intelligent Q&A system
- Two or three concise capability points
- The provided company logo image as a secondary logo near the heading area

The visual tone should be clean and readable, with enough contrast against the background image.

### Right login area

The right side should remain minimal:

- Login title
- Email field
- Password field
- Login button

No additional action buttons or backend-related controls should be introduced. The form can sit inside a soft white or translucent white card with rounded corners and restrained elevation.

### Background treatment

Preferred approach:

- Use a single shared background image for the full page
- Apply a light overlay, blur effect, or wash if needed so text and inputs remain readable
- Avoid a visually obvious split between left and right sections

Fallback approach:

- Replace the current background with a better-fit lighter enterprise image if the supplied one cannot be tuned to the desired tone

### Responsive behavior

On smaller widths:

- Collapse to a single-column layout
- Keep the login form as the primary focus
- Either reduce the left-side copy or stack it above the form without overwhelming the viewport
- Preserve readability and input usability without horizontal overflow

## Implementation Scope

Primary files:

- `admin-ui/src/views/Login.js`
- `admin-ui/style.css`

Optional supporting work:

- Add or reference one local image asset for the background
- Add or reference the provided company logo asset in a safe local path if direct external-path loading is not appropriate for runtime use

## Content Guidance

Suggested tone:

- Professional
- Internal enterprise system
- Clear and concise

Suggested left-side copy shape:

- One main heading
- One short supporting sentence
- Two or three short capability bullets

The exact wording may be refined during implementation, but it must stay short and avoid adding promises that the product does not currently support.

## Technical Notes

- Preserve the current `submit()` behavior in `Login.js`
- Preserve current imports and route handling unless asset imports require minimal additions
- Keep styling changes localized to the login page selectors where practical
- Maintain current mobile behavior quality or improve it

## Acceptance Criteria

- The login page uses a unified full-page background image rather than a hard split background.
- The left side presents brand copy with the current product title as the main title.
- The provided company logo is included as an auxiliary brand element.
- The right side still contains only email, password, and login button controls.
- Existing login behavior works as before, including redirect handling.
- No new backend-dependent actions are introduced.
- The page remains visually coherent on desktop and mobile widths.

## Verification Plan

- Open the `admin-ui` login page locally and verify the new composition on desktop width.
- Check a narrow/mobile width layout for readability and spacing.
- Confirm that form submission wiring is unchanged by reviewing the component logic.
- If available in the local workflow, run the relevant lightweight UI tests that touch login-page rendering or preview behavior.

## Risks And Mitigations

- Background image too dark:
  Use a light overlay or replace the image.
- Auxiliary logo visually overpowers the product name:
  Keep the logo smaller and secondary to the title.
- Responsive layout becomes crowded:
  Reduce left-side content density and prioritize the form on small screens.
- Existing in-progress workspace changes conflict with login styling:
  Limit edits to login-focused selectors and review diffs carefully before staging.
