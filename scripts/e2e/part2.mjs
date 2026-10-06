// Staff journey: ACM review -> balance -> event control -> announcement -> judging -> release.
import { chromium } from "playwright-core"
import { readFileSync } from "node:fs"

const A = "http://localhost:3001"
const P = "http://localhost:3000"
const B = "http://127.0.0.1:8000"
const shots = process.env.E2E_DIR ?? new URL(".", import.meta.url).pathname.replace(/\/$/, "")
const staff = JSON.parse(readFileSync(`${shots}/staff.json`, "utf8"))
const state = JSON.parse(readFileSync(`${shots}/state.json`, "utf8"))
const log = (...args) => console.log("•", ...args)
const ok = (condition, message) => { if (!condition) throw new Error(`FAIL: ${message}`); log("ok:", message) }

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH })
const adminContext = await browser.newContext({ viewport: { width: 1400, height: 950 } })
const admin = await adminContext.newPage()
const participantContext = await browser.newContext({ viewport: { width: 1280, height: 900 }, storageState: `${shots}/participant-state.json` })
const team = await participantContext.newPage()
const errors = []
for (const page of [admin, team]) page.on("console", (message) => { if (message.type() === "error") errors.push(`${page === admin ? "admin" : "team"}: ${message.text()}`) })

async function login(page, email) {
  await page.goto(`${A}/login`)
  await page.fill("#email", email)
  await page.fill("#password", "password")
  await page.getByRole("button", { name: /sign in|log in|login/i }).click()
  await page.waitForURL(/\/dashboard/, { timeout: 20_000 })
}

try {
  // 1. Overview loads for a super admin with live metrics.
  await login(admin, staff.super)
  await admin.getByText("Captured payments, net of refunds").waitFor()
  ok(true, "super admin overview loads with live metrics")

  // 2. Registrations: find the team, open the drawer, reject the ACM claim.
  await admin.goto(`${A}/dashboard/registrations`)
  await admin.getByPlaceholder("Team or leader email").fill(state.team)
  const row = admin.getByRole("row", { name: `Open ${state.team}` })
  await row.waitFor()
  ok(await row.getByText("1 ACM to check").isVisible(), "registrations row flags the pending ACM check")
  await row.click()
  const drawer = admin.getByRole("dialog")
  await drawer.getByText("Members and ACM review").waitFor()
  ok(await drawer.getByText("failed").first().isVisible() && await drawer.getByText("captured").first().isVisible(), "drawer lists the failed attempt and the captured payment")
  await admin.screenshot({ path: `${shots}/10-admin-drawer.png` })
  await drawer.getByRole("button", { name: "Reject" }).click()
  await drawer.getByRole("button", { name: "Reject claim" }).click()
  await drawer.getByText("Claim rejected; the leader was emailed any balance due.").waitFor()
  await drawer.getByText(/The team owes ₹200 after ACM review/).waitFor()
  ok(true, "ACM rejection creates a ₹200 balance (₹400 standard - ₹200 paid)")
  await admin.keyboard.press("Escape")

  // 3. Participant sees the balance, pays it, and the block lifts.
  await team.goto(`${P}/dashboard`)
  await team.getByText(/Pay the ₹200 balance to unlock final submission/).waitFor({ timeout: 20_000 })
  ok(true, "participant dashboard shows the balance due without a reload")
  const order = await team.evaluate(async () => (await fetch("/api/participant/payment-adjustment/order", { method: "POST" })).json())
  ok(order.amount === 20000 && order.purpose === "ADJUSTMENT", "balance order is created server-side for ₹200")
  const payment = await (await fetch(`${B}/__e2e/pay`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ order_id: order.order_id, amount: 20000 }) })).json()
  const form = new FormData()
  form.set("razorpay_order_id", order.order_id)
  form.set("razorpay_payment_id", payment.id)
  form.set("razorpay_signature", "good")
  const settled = await fetch(`${P}/api/payments/callback?for=balance`, { method: "POST", body: form, redirect: "manual" })
  ok(settled.headers.get("location").endsWith("/dashboard?balance=paid"), "balance callback returns to the dashboard")
  await team.goto(`${P}/dashboard?balance=paid`)
  await team.getByText("Balance payment received. Thank you!").waitFor()
  ok(!(await team.getByText(/balance to unlock final submission/).isVisible()), "balance banner disappears once paid")

  // 4. Event control + challenge: reveal the problem statement.
  await admin.goto(`${A}/dashboard/challenge`)
  await admin.getByLabel("Title").fill("Resilient campuses")
  await admin.getByLabel("One-line summary").fill("Keep a campus connected when the network fails.")
  await admin.getByLabel("Full brief").fill("Design a system that keeps critical campus services usable offline.\nJudging favours working demos.")
  await admin.getByRole("button", { name: "Save" }).click()
  await admin.getByText(/Version \d+, saved/).waitFor()
  await admin.goto(`${A}/dashboard/event-control`)
  const revealInput = admin.getByLabel("Problem statement reveal (IST)")
  const current = await revealInput.inputValue()
  let reveal = new Date(Date.now() - 60_000 + 330 * 60_000).toISOString().slice(0, 16)
  if (reveal === current) reveal = new Date(Date.now() - 120_000 + 330 * 60_000).toISOString().slice(0, 16)
  await revealInput.fill(reveal)
  await admin.getByRole("button", { name: "Save changes" }).click()
  await admin.getByText("revealed").first().waitFor({ timeout: 15_000 })
  ok(true, "event control saves the reveal time and shows it as revealed")
  await team.goto(`${P}/dashboard`)
  await team.getByText("Keep a campus connected when the network fails.").waitFor({ timeout: 20_000 })
  ok(true, "participant sees the released problem statement")

  // 5. Announcement with email; bell shows it as unread.
  await admin.goto(`${A}/dashboard/announcements`)
  await admin.getByRole("button", { name: "New announcement" }).click()
  const announcementTitle = `Mentors at 4 PM ${Date.now() % 10000}`
  await admin.getByLabel("Title").fill(announcementTitle)
  await admin.getByLabel(/^Message/).fill("Mentor office hours in Lab 2.")
  await admin.getByRole("button", { name: "Save draft" }).click()
  const item = admin.getByRole("listitem").filter({ hasText: announcementTitle }).first()
  await item.getByRole("button", { name: "Publish" }).click()
  await item.getByRole("button", { name: "Publish now" }).click()
  await admin.getByText("Published to all teams.").waitFor()
  await team.reload()
  await team.getByRole("button", { name: /Announcements, \d+ unread/ }).click()
  await team.getByText(announcementTitle).first().waitFor()
  ok(true, "participant bell shows the new announcement")
  await team.screenshot({ path: `${shots}/11-team-bell.png` })
  await team.keyboard.press("Escape")

  // 6. Judging round: create, open, assign one judge to this team.
  await admin.goto(`${A}/dashboard/leaderboard`)
  await admin.getByRole("button", { name: "New round" }).click()
  const roundName = `Finals ${Date.now() % 100000}`
  await admin.getByPlaceholder("Finals").fill(roundName)
  await admin.getByRole("button", { name: "Create" }).click()
  await admin.getByText("Round created.").waitFor()
  await admin.getByRole("heading", { name: roundName }).waitFor()
  await admin.getByRole("button", { name: "Open round" }).click()
  await admin.getByRole("button", { name: "Open for scoring" }).click()
  await admin.getByText("Round opened; judges can score now.").waitFor()
  const teamRow = admin.getByRole("row").filter({ hasText: state.team })
  await teamRow.getByLabel(`Add a judge for ${state.team}`).selectOption({ label: new RegExp("") , index: 1 }).catch(async () => {
    const options = await teamRow.getByLabel(`Add a judge for ${state.team}`).locator("option").allTextContents()
    const judgeOption = options.find((text) => text.startsWith(`Staff ${staff.judge.slice(6, 12)}`))
    await teamRow.getByLabel(`Add a judge for ${state.team}`).selectOption({ label: judgeOption })
  })
  await admin.getByText("Judge assigned.").waitFor()
  ok(true, "judge assigned to the team from the judging board")
  await admin.screenshot({ path: `${shots}/12-rounds.png`, fullPage: true })

  // 7. Judge scores anonymously.
  const judgeContext = await browser.newContext({ viewport: { width: 1280, height: 900 } })
  const judge = await judgeContext.newPage()
  const assignedEmail = await teamRow.locator("span", { hasText: "Staff" }).first().textContent()
  const judgeEmail = [staff.judge, staff.judge2].find((email) => assignedEmail.includes(`Staff ${email.slice(6, 12)}`))
  await login(judge, judgeEmail)
  await judge.waitForURL(`${A}/dashboard/judging`, { timeout: 20_000 })
  ok(true, "judge-only account lands on the assignment queue")
  const assignment = judge.locator("section", { hasText: roundName }).getByRole("link").filter({ hasText: "Signal Mesh" }).first()
  await assignment.click()
  await judge.getByText("Repository links can reveal usernames").waitFor()
  ok(!(await judge.getByText(state.team).isVisible()), "judge view never shows the team name")
  for (const label of ["Innovation", "Technical implementation", "Problem solving", "UI/UX", "Completeness"]) {
    await judge.getByLabel(new RegExp(`^${label.replace("/", "\\/")} \\(0`)).fill("8")
  }
  await judge.getByRole("button", { name: "Submit", exact: true }).click()
  await judge.getByRole("button", { name: "Submit final scores" }).click()
  await judge.getByText("Scores submitted. Thank you!").waitFor()
  ok(true, "judge submits a complete scorecard")
  await judge.screenshot({ path: `${shots}/13-judge.png`, fullPage: true })

  // 8. Generate, lock, release; participant and public see results.
  await admin.reload()
  await admin.getByRole("button", { name: "Generate" }).click()
  await admin.getByText("Results generated from submitted scorecards.").waitFor()
  await admin.getByRole("button", { name: "Lock" }).click()
  await admin.getByRole("button", { name: "Lock results" }).click()
  await admin.getByText("Results locked.").waitFor()
  await admin.getByRole("button", { name: "Release" }).click()
  await admin.getByRole("button", { name: "Release publicly" }).click()
  await admin.getByText("Results released to teams and the public leaderboard.").waitFor()
  ok(true, "results generated, locked, and released")
  await team.goto(`${P}/dashboard`)
  await team.getByText(/Your team is ranked/).waitFor({ timeout: 20_000 })
  ok(true, "participant sees the released rank")
  await team.getByText(/Your team is ranked/).scrollIntoViewIfNeeded()
  await team.waitForTimeout(1500)
  await team.screenshot({ path: `${shots}/14-team-results.png` })
  await team.goto(`${P}/leaderboard`)
  await team.getByText(state.team).waitFor()
  ok(true, "public leaderboard lists the team")
  await team.screenshot({ path: `${shots}/15-public-leaderboard.png` })

  // 9. Partial refund from the drawer, idempotent and audited.
  await admin.goto(`${A}/dashboard/registrations`)
  await admin.getByPlaceholder("Team or leader email").fill(state.team)
  await admin.getByRole("row", { name: `Open ${state.team}` }).click()
  const refundDrawer = admin.getByRole("dialog")
  await refundDrawer.getByRole("button", { name: "Refund…" }).first().click()
  await refundDrawer.getByLabel(/^Amount/).fill("50")
  await refundDrawer.getByLabel(/^Reason/).fill("Goodwill discount")
  await refundDrawer.getByRole("button", { name: "Refund", exact: true }).click()
  await refundDrawer.getByRole("button", { name: "Refund ₹50" }).click()
  await refundDrawer.getByText("Refund sent to Razorpay.").waitFor()
  await refundDrawer.getByText("₹50 · Goodwill discount").waitFor()
  ok(true, "partial refund recorded and shown in the drawer")
  await admin.screenshot({ path: `${shots}/16-refund.png` })
} catch (error) {
  await admin.screenshot({ path: `${shots}/fail-admin.png` }).catch(() => undefined)
  await team.screenshot({ path: `${shots}/fail-team.png` }).catch(() => undefined)
  console.log("console errors:", errors.slice(0, 15))
  await browser.close()
  throw error
}
console.log("console errors:", errors.filter((text) => !text.includes("favicon")).slice(0, 15))
await browser.close()
