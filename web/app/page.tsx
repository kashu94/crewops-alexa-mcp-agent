// Opening the app always starts at sign-in; whoever is already signed in in this browser
// is offered "Continue" there.
import { redirect } from "next/navigation";

export default function Home() {
  redirect("/login");
}
