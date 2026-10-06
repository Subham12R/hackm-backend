// Participant journey: register -> failed payment -> retry -> captured -> activate -> dashboard.
import { chromium } from "playwright-core"
import { readFileSync, writeFileSync, existsSync } from "node:fs"

const P = "http://localhost:3000"
const B = "http://127.0.0.1:8000"
const shots = process.env.E2E_DIR ?? new URL(".", import.meta.url).pathname.replace(/\/$/, "")
const log = (...args) => console.log("•", ...args)
const assert = (condition, message) => { if (!condition) { console.error("✗ FAIL:", message); process.exitCode = 1; throw new Error(message) } else log("ok:", message) }

const id = Math.random().toString(36).slice(2, 8)
const leader = `leader-${id}@example.com`
const payload = {
  team_name: `Pixel Pirates ${id}`, rules_consent: true, privacy_consent: true,
  members: [
    { role: "leader", full_name: "Asha Roy", email: leader, phone: "9812345678", college: "Adamas University", is_acm_member: true, acm_id: "ACM-77" },
    { role: "member", full_name: "Rahul Sen", email: `member-${id}@example.com`, phone: "9812345679", college: "Jadavpur University", is_acm_member: false, acm_id: "" },
  ],
}

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH })
const context = await browser.newContext({ viewport: { width: 1280, height: 900 } })
const page = await context.newPage()
const consoleErrors = []
page.on("console", (message) => { if (message.type() === "error") consoleErrors.push(message.text()) })

try {
// 1. Registration through the participant BFF (same-origin call from the page).
await page.goto(`${P}/register`)
const created = await page.evaluate(async (body) => {
  const response = await fetch("/api/registrations", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) })
  return { status: response.status, data: await response.json() }
}, payload)
assert(created.status === 202 && created.data.amount === 20000, `registration quote is server-priced ₹200 (got ${created.status} ${JSON.stringify(created.data)})`)
const invalid = await page.evaluate(async (body) => {
  const response = await fetch("/api/registrations", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) })
  return { status: response.status, data: await response.json() }
}, { ...payload, team_name: `Other ${id}`, members: [{ ...payload.members[0], acm_id: "" }, payload.members[1]] })
assert(invalid.status === 422 && /ACM ID/.test(invalid.data.error), `missing ACM ID gets a readable 422: "${invalid.data.error}"`)

// 2. Pay page: a failed payment leaves Pay enabled with the reason shown.
await page.evaluate((order) => sessionStorage.setItem("hackmatrix.payment-order", JSON.stringify(order)), created.data)
const failed = await (await fetch(`${B}/__e2e/pay`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ order_id: created.data.order_id, amount: 20000, status: "failed" }) })).json()
const failedForm = new FormData()
failedForm.set("error[code]", "BAD_REQUEST_ERROR")
failedForm.set("error[description]", "Card declined by bank")
failedForm.set("error[metadata]", JSON.stringify({ order_id: created.data.order_id, payment_id: failed.id }))
const failedRedirect = await fetch(`${P}/api/payments/callback`, { method: "POST", body: failedForm, redirect: "manual" })
const failedLocation = failedRedirect.headers.get("location")
assert(failedLocation.includes("/register/pay?failed=1"), `failed callback redirects to retry page (${failedLocation})`)
await page.goto(failedLocation.replace("http://127.0.0.1:3000", P))
await page.getByText("Card declined by bank").waitFor()
const retryButton = page.getByRole("button", { name: "Try payment again" })
assert(await retryButton.isEnabled(), "Pay stays enabled after a failed payment")
await page.screenshot({ path: `${shots}/01-pay-failed.png` })

// 3. Captured payment whose browser callback was lost: the unconfirmed page polls and moves on.
await fetch(`${B}/__e2e/pay`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ order_id: created.data.order_id, amount: 20000 }) })
await page.goto(`${P}/register/pay?unconfirmed=1&order=${created.data.order_id}`)
await page.waitForURL(`${P}/activate`, { timeout: 30_000 })
assert(true, "unconfirmed page reconciles the captured payment and moves to /activate")
await page.screenshot({ path: `${shots}/02-check-email.png` })

// 4. Activation link from the email outbox.
const emails = readFileSync(`${shots}/emails.jsonl`, "utf8").trim().split("\n").map((line) => JSON.parse(line))
const activation = emails.reverse().find((mail) => mail.to === leader && mail.subject.includes("Activate"))
assert(activation, "activation email was delivered through the outbox")
const link = activation.html.match(/http:\/\/127\.0\.0\.1:3000\/activate\?token=[^"&<\s]+/)[0].replace("http://127.0.0.1:3000", P)
await page.goto(link)
await page.locator("#auth2-email").and(page.locator(`[value="${leader}"]`)).waitFor()
await page.fill("#auth2-password", "supersecret1")
await page.getByRole("button").filter({ hasText: /activate|create|continue|set/i }).first().click()
await page.waitForURL(`${P}/dashboard`, { timeout: 20_000 })
await page.getByText(`${payload.team_name} Dashboard`).waitFor({ timeout: 20_000 })
assert(true, "activation signs the leader in and opens the live dashboard")

// 5. Dashboard content is real.
await page.getByText("Unverified").waitFor()
await page.getByText("Your team is being verified...").waitFor()
await page.getByText("Rahul Sen").waitFor()
await page.getByText("ACM check pending").waitFor()
await page.screenshot({ path: `${shots}/03-dashboard-top.png`, fullPage: false })

// 6. Submission: validation, draft save, final submit with type-SUBMIT.
await page.fill("#project-name", "Signal Mesh")
await page.fill("#project-github_url", "https://gitlab.com/acme/signal")
await page.getByText("Use a github.com repository link.").waitFor()
await page.fill("#project-github_url", "https://github.com/acme/signal-mesh")
await page.fill("#project-pitch_deck_url", "https://drive.google.com/file/d/deck")
await page.fill("#project-documentation_url", "https://notion.so/signal-writeup")
await page.fill("#project-description", "Offline-first alerts over a mesh of phones.")
await page.getByRole("button", { name: "Save draft" }).click()
await page.getByText("Draft saved. You can keep editing until you submit.").waitFor()
await page.getByRole("button", { name: "Submit project" }).click()
const confirm = page.getByRole("button", { name: "Submit final project" })
assert(await confirm.isDisabled(), "final submit stays disabled until SUBMIT is typed")
await page.getByLabel("Type SUBMIT to confirm").fill("SUBMIT")
await confirm.click()
await page.getByText(/Submitted .* Your project is locked for judging/).waitFor({ timeout: 15_000 })
assert(await page.locator("#project-name").isDisabled(), "finalized submission is read-only")
await page.screenshot({ path: `${shots}/04-submitted.png`, fullPage: true })

// 7. Member edit (non-price field).
await page.getByRole("button", { name: "Edit details" }).nth(1).click()
await page.fill("#member-phone", "+91 98300 11111")
await page.getByRole("button", { name: "Save", exact: true }).click()
await page.getByRole("dialog").waitFor({ state: "hidden" })
assert(true, "member phone edit saves and closes the dialog")

// 8. Logout revokes the session; the dashboard then requires sign-in.
await page.getByRole("button", { name: "Log out" }).click()
await page.waitForURL(/\/login/)
await page.goto(`${P}/dashboard`)
await page.waitForURL(/\/login/)
assert(true, "after logout the dashboard redirects to login")

// 9. Sign back in for later parts.
await page.fill("input[type=email]", leader)
await page.fill("input[type=password]", "supersecret1")
await page.getByRole("button").filter({ hasText: /sign in|log in|login|continue/i }).first().click()
await page.waitForURL(`${P}/dashboard`, { timeout: 20_000 })
assert(true, "leader signs in again with the new password")

writeFileSync(`${shots}/state.json`, JSON.stringify({ leader, team: payload.team_name, password: "supersecret1", ...(existsSync(`${shots}/state.json`) ? {} : {}) }))
await context.storageState({ path: `${shots}/participant-state.json` })
} catch (error) {
  await page.screenshot({ path: `${shots}/failure.png` })
  await page.locator("section", { hasText: "Submit your project here!" }).screenshot({ path: `${shots}/failure-section.png` }).catch(() => undefined)
  console.log("console errors:", consoleErrors.slice(0, 10))
  await browser.close()
  throw error
}
console.log("console errors:", consoleErrors.filter((text) => !text.includes("favicon")).slice(0, 10))
await browser.close()
