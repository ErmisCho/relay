import { useState, type FormEvent } from "react";
import { ApiError, type RelayApi } from "../api/client";

export function PasscodeGate({ api, onUnlocked }: { api: RelayApi; onUnlocked: () => void }) {
  const [passcode, setPasscode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!passcode.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      if (await api.login(passcode.trim())) onUnlocked();
      else setError("That passcode is not right. Ask the person who shared this demo.");
    } catch (err) {
      setError(
        err instanceof ApiError && err.code === "rate_limited"
          ? "Too many attempts. Wait a minute and try again."
          : "The relay server cannot be reached right now.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="gate" id="main">
      <form className="gate__card" onSubmit={submit} aria-describedby="gate-about">
        <p className="brand">relay</p>
        <h1 className="gate__title">A voice agent you think out loud with</h1>
        <p id="gate-about" className="muted">
          It only starts background work after it reads the plan back and hears a clear yes. This demo shows every decision
          as it happens.
        </p>
        <label htmlFor="passcode" className="field-label">
          Demo passcode
        </label>
        <input
          id="passcode"
          className="input"
          type="password"
          autoComplete="current-password"
          value={passcode}
          onChange={(e) => setPasscode(e.target.value)}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? "gate-error" : undefined}
          autoFocus
        />
        {error && (
          <p id="gate-error" className="tone-danger small" role="alert">
            {error}
          </p>
        )}
        {api.passcodeHint && <p className="mono faint small">Mock mode: the passcode is {api.passcodeHint}</p>}
        <button type="submit" className="btn btn--accent btn--block" disabled={busy}>
          {busy ? "Checking…" : "Enter"}
        </button>
      </form>
    </main>
  );
}
