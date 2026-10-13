import { describe, expect, it } from "vitest";

import { answerRoomQuestion, OUT_OF_SCOPE, type RoomBoard } from "@/lib/room-answers";

const board: RoomBoard = {
  counts: { open: 3, high: 1, medium: 1, low: 1, escalated: 1 },
  desks: [
    { desk_id: "desk-1", controller: "Ananya Iyer", open: 0, high: 0, escalated: 0, submitted: 0 },
    { desk_id: "desk-2", controller: "Rohit Malhotra", open: 2, high: 1, escalated: 1, submitted: 1 },
    { desk_id: "desk-3", controller: "Divya Rao", open: 1, high: 0, escalated: 0, submitted: 0 },
  ],
  disruptions: [
    {
      what: "A. Nair (C-1042) out 14 Oct",
      status: "OPTIONS_READY",
      severity: "high",
      escalated: true,
      desk: { desk_id: "desk-2", controller: "Rohit Malhotra" },
    },
    {
      what: "S. Nair (C-2295) out 16 Oct",
      status: "SUBMITTED",
      severity: "medium",
      escalated: false,
      desk: { desk_id: "desk-2", controller: "Rohit Malhotra" },
    },
    {
      what: "Gate 4 closed",
      status: "OPTIONS_READY",
      severity: "low",
      escalated: false,
      desk: { desk_id: "desk-3", controller: "Divya Rao" },
    },
  ],
  day: { flights: 21, passengers: 2272, crew_out: 2, reserves_on_call: 6 },
};

const say = (q: string, scope = "room") => answerRoomQuestion(q, board, scope).text;

describe("desk speaker questions", () => {
  it("counts desks and names the controllers", () => {
    expect(say("How many desks are there?")).toBe(
      "There are 3 desks: desk 1 with Ananya Iyer, desk 2 with Rohit Malhotra and desk 3 with Divya Rao.",
    );
    expect(say("how many controllers")).toBe("There are 3 controllers, one per desk.");
    expect(say("Who are the controllers?")).toContain("Ananya Iyer on desk 1, Rohit Malhotra on desk 2 and Divya Rao");
  });

  it("gives each desk's load and the busiest one", () => {
    expect(say("Which desk has what?")).toBe(
      "Ananya Iyer has 0 open, Rohit Malhotra has 2 open, 1 high and Divya Rao has 1 open.",
    );
    expect(say("how many does each controller have")).toContain("Rohit Malhotra has 2 open");
    expect(say("Which desk is busiest?")).toMatch(/^Rohit Malhotra, desk 2, has 2 open disruptions/);
  });

  it("answers about one desk by name, number or 'my desk'", () => {
    expect(say("What's on Rohit's desk?")).toBe(
      "Rohit Malhotra, desk 2, has 2 open disruptions: 1 high severity, 1 escalated, 1 waiting for approval. " +
        "The most urgent: A. Nair (C-1042) out 14 Oct, high severity.",
    );
    expect(say("what does desk one have")).toBe("Ananya Iyer, desk 1, has nothing open.");
    expect(say("what's on my desk", "desk-3")).toMatch(/^Divya Rao, desk 3, has 1 open disruption\b/);
  });

  it("totals, escalations, approvals and today's numbers", () => {
    expect(say("How many disruptions are open?")).toBe(
      "3 disruptions are open: 1 high, 1 medium, 1 low, and 1 escalated.",
    );
    expect(say("anything escalated?")).toBe("1 disruption is escalated: A. Nair (C-1042) out 14 Oct.");
    expect(say("any plans waiting for approval")).toBe("1 plan is waiting for approval.");
    expect(say("how many flights today")).toBe("21 flights today, with 2,272 passengers booked.");
  });

  it("turns away anything else", () => {
    const r = answerRoomQuestion("Is C-2087 legal for P-2291?", board, "room");
    expect(r).toEqual({ text: OUT_OF_SCOPE, inScope: false });
    expect(answerRoomQuestion("tell me a joke", board).inScope).toBe(false);
  });
});
