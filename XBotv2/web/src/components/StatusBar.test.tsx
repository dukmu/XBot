import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { initialRuntimeState } from "../state/runtime";
import { StatusBar } from "./StatusBar";

it("distinguishes initial connection from an unavailable server", () => {
  const { rerender } = render(<StatusBar state={initialRuntimeState} />);
  expect(screen.getByText("Connecting…")).toBeVisible();
  expect(screen.queryByText("Server unavailable")).toBeNull();
  rerender(<StatusBar state={{ ...initialRuntimeState, loading: false }} />);
  expect(screen.getByText("Server unavailable")).toBeVisible();
});
