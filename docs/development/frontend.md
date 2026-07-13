# Frontend Architecture

The user interface is built using **Vite**, **React 19**, **TypeScript**, and styled with **Tailwind CSS** + **shadcn/ui**.

---

## Folder Structure

All client code resides in `frontend/src/`:

- **`components/`**: Atomic UI parts (buttons, logs, cards, layouts) and progress indicators.
- **`hooks/`**:
  - `useConversionQueue.tsx`: Owns conversion queue state, backend job recovery, SSE/polling observers, cancellation, downloads, and retry flows.
- **`pages/`**:
  - `OnboardingPage.tsx`: Gatekeeps the application; monitors neural weight downloads.
  - `ConvertPage.tsx`: Drag-and-drop zone and parameters configuration.
  - `HistoryPage.tsx`: Pagination and file download table.
  - `SettingsPage.tsx`: Forms for LLM API keys and database purge options.

---

## Styling & Layout Rules

- **Theme**: Light and Dark mode using custom Tailwind classes and standard HSL variables.
- **Feedback**: Actions and failures are flagged with inline notifications and `sonner` toasts.
- **Responsive Layout**: Designed to work gracefully on desktops and tablets.
