import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { InjectForm } from "./InjectForm";

const personaName = (id: string) =>
  ({ buffett: "Warren Buffett", lynch: "Peter Lynch" }[id] ?? id);

function injectMutStub() {
  return { mutate: vi.fn(), isPending: false, isError: false, error: null };
}

function interjectMutStub(overrides: Partial<{
  isPending: boolean;
  isError: boolean;
  isSuccess: boolean;
  data: { status: "queued" | "answered" };
}> = {}) {
  return {
    mutate: vi.fn(),
    isPending: false,
    isError: false,
    isSuccess: false,
    error: null,
    data: undefined,
    ...overrides,
  };
}

function baseProps() {
  return {
    injectDraft: "",
    setInjectDraft: vi.fn(),
    injectMut: injectMutStub(),
    setInjectSheetOpen: vi.fn(),
  };
}

describe("InjectForm", () => {
  it("between mode (default) submits the trimmed draft via injectMut", () => {
    const props = baseProps();
    render(<InjectForm {...props} injectDraft="  聚焦 2330  " />);
    fireEvent.click(screen.getByRole("button", { name: "Inject" }));
    expect(props.injectMut.mutate).toHaveBeenCalledWith("聚焦 2330");
  });

  it("between mode shows no persona select", () => {
    render(<InjectForm {...baseProps()} />);
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("running mode submits question + selected persona via interjectMut", () => {
    const props = baseProps();
    const interjectMut = interjectMutStub();
    render(
      <InjectForm
        {...props}
        mode="running"
        interjectMut={interjectMut}
        personaIds={["buffett", "lynch"]}
        personaName={personaName}
        interjectTarget="lynch"
        setInterjectTarget={vi.fn()}
        injectDraft="2330 下檔風險？"
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Ask now" }));
    expect(interjectMut.mutate).toHaveBeenCalledWith({
      question: "2330 下檔風險？",
      target_persona: "lynch",
    });
    expect(props.injectMut.mutate).not.toHaveBeenCalled();
  });

  it("running mode omits target_persona when the moderator-assigns option is kept", () => {
    const interjectMut = interjectMutStub();
    render(
      <InjectForm
        {...baseProps()}
        mode="running"
        interjectMut={interjectMut}
        personaIds={["buffett", "lynch"]}
        personaName={personaName}
        interjectTarget=""
        setInterjectTarget={vi.fn()}
        injectDraft="請評估風險"
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Ask now" }));
    expect(interjectMut.mutate).toHaveBeenCalledWith({
      question: "請評估風險",
      target_persona: undefined,
    });
  });

  it("renders the roster in the persona select with a moderator default", () => {
    render(
      <InjectForm
        {...baseProps()}
        mode="running"
        interjectMut={interjectMutStub()}
        personaIds={["buffett", "lynch"]}
        personaName={personaName}
        interjectTarget=""
        setInterjectTarget={vi.fn()}
      />,
    );
    const options = screen.getAllByRole("option").map((o) => o.textContent);
    expect(options).toEqual([
      "Moderator assigns",
      "Warren Buffett",
      "Peter Lynch",
    ]);
  });

  it("shows the queued confirmation after a running-mode enqueue", () => {
    render(
      <InjectForm
        {...baseProps()}
        mode="running"
        interjectMut={interjectMutStub({
          isSuccess: true,
          data: { status: "queued" },
        })}
        personaIds={["buffett"]}
        personaName={personaName}
        setInterjectTarget={vi.fn()}
      />,
    );
    expect(
      screen.getByText("Queued — it will be answered at the next turn boundary"),
    ).toBeInTheDocument();
  });

  it("followup mode renders the 追問 label", () => {
    render(
      <InjectForm
        {...baseProps()}
        mode="followup"
        interjectMut={interjectMutStub()}
        personaIds={["buffett"]}
        personaName={personaName}
        setInterjectTarget={vi.fn()}
      />,
    );
    expect(
      screen.getByText("Follow-up — ask one more question about the conclusion"),
    ).toBeInTheDocument();
  });
});
