import { describe, expect, it } from "vitest";
import { getHonestyMessage } from "./LoadingView";

describe("getHonestyMessage", () => {
  it("labels fallback results as a backup path", () => {
    expect(getHonestyMessage("fallback")).toContain("backup cooking path");
    expect(getHonestyMessage("fallback")).not.toContain("live decision model selected");
  });

  it("labels mock results as a backup even when the source field is present", () => {
    expect(getHonestyMessage("decider_2b", true)).toContain("backup cooking path");
  });

  it("only describes the live model as selecting a path for a genuine result", () => {
    expect(getHonestyMessage("decider_2b", false)).toContain("live decision model selected");
  });

  it("uses neutral copy while the decision source is not known", () => {
    expect(getHonestyMessage()).toContain("live decision model or a backup path");
  });
});
