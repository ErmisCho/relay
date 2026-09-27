import { createHttpApi, type RelayApi } from "./client";

/**
 * VITE_API_MODE=mock serves everything from the in-browser mock; anything else
 * talks to the Delegator. The mock is imported dynamically so a live build
 * does not ship it.
 */
export async function createApi(): Promise<RelayApi> {
  if (import.meta.env.VITE_API_MODE === "mock") {
    const { createMockApi } = await import("./mock");
    return createMockApi();
  }
  return createHttpApi();
}
