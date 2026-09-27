import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Markdown } from "./Markdown";

describe("Markdown (executor-written briefs are untrusted)", () => {
  it("does not render raw HTML or script-capable links", () => {
    const { container } = render(
      <Markdown
        source={[
          "<script>window.pwned = 1</script>",
          '<img src="x" onerror="window.pwned = 1">',
          "[bad](javascript:alert(1)) [data](data:text/html,hi) [good](https://example.org/a)",
          "![pixel](https://tracker.example/p.gif)",
        ].join("\n\n")}
      />,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    const hrefs = [...container.querySelectorAll("a")].map((a) => a.getAttribute("href"));
    expect(hrefs).toEqual(["https://example.org/a"]);
    const good = container.querySelector("a")!;
    expect(good.getAttribute("rel")).toBe("noopener noreferrer");
    expect(good.getAttribute("target")).toBe("_blank");
  });
});
