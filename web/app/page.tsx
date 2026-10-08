"use client";

import { useEffect } from "react";

import { Spinner } from "@/components/ui";
import { tool } from "@/lib/client";
import { homeFor, type Me } from "@/lib/roles";

export default function Home() {
  useEffect(() => {
    tool<Me>("whoami").then(
      (me) => window.location.replace(homeFor(me.roles)),
      () => undefined, // signed out: the client sends the browser to /login
    );
  }, []);
  return (
    <main className="flex flex-1 items-center justify-center text-muted">
      <Spinner />
    </main>
  );
}
