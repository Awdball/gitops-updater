#!/usr/bin/env python3
"""Lightweight Telegram ChatOps Bot for gitops-updater."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from updater import ApplyResult, GitOpsEngine, ScanResult

logger = logging.getLogger("gitops_updater.bot")


class TelegramBot:
    def __init__(
        self,
        token: str,
        allowed_chat_ids: set[int],
        engine: GitOpsEngine,
        cluster_name: str = "Kubernetes Cluster",
        schedule_time_str: str = "04:00",
        schedule_delay_seconds: float = 120.0,
        manual_delay_seconds: float = 0.5,
        notify_on_no_updates: bool = False,
    ):
        self.token = token
        self.allowed_chat_ids = allowed_chat_ids
        self.engine = engine
        self.cluster_name = cluster_name
        self.schedule_time_str = schedule_time_str
        self.schedule_delay_seconds = schedule_delay_seconds
        self.manual_delay_seconds = manual_delay_seconds
        self.notify_on_no_updates = notify_on_no_updates
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.last_update_id = 0
        self.latest_scan: ScanResult | None = None
        self.is_scanning = False
        self.is_applying = False
        self._stop_event = threading.Event()

    def _request(self, method: str, payload: dict[str, Any] | None = None, timeout: int = 40) -> dict[str, Any]:
        url = f"{self.base_url}/{method}"
        headers = {"Content-Type": "application/json"}
        data = json.dumps(payload).encode("utf-8") if payload else None
        req = Request(url, data=data, headers=headers)
        with urlopen(req, timeout=timeout) as resp:
            return json.load(resp)

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> int | None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            res = self._request("sendMessage", payload)
            return res.get("result", {}).get("message_id")
        except Exception as exc:
            logger.error(f"Failed to send Telegram message to {chat_id}: {exc}")
            return None

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            self._request("editMessageText", payload)
        except Exception as exc:
            logger.error(f"Failed to edit Telegram message {message_id}: {exc}")

    def answer_callback(self, callback_query_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        try:
            self._request("answerCallbackQuery", payload)
        except Exception as exc:
            logger.error(f"Failed to answer callback query: {exc}")

    def broadcast(
        self,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> None:
        for chat_id in self.allowed_chat_ids:
            self.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)

    def is_authorized(self, user_id: int, chat_id: int) -> bool:
        if user_id in self.allowed_chat_ids or chat_id in self.allowed_chat_ids:
            return True
        logger.warning(f"Unauthorized access attempt by user_id={user_id}, chat_id={chat_id}")
        self.send_message(
            chat_id,
            f"⛔ <b>Access Denied</b>\nYour Telegram ID (<code>{user_id}</code>) is not authorized to interact with this cluster.",
        )
        return False

    def handle_command(self, chat_id: int, user_id: int, text: str) -> None:
        cmd = text.split()[0].lower() if text else ""
        if "@" in cmd:
            cmd = cmd.split("@")[0]

        if cmd in {"/start", "/help"}:
            msg = (
                "🤖 <b>GitOps Updater Bot</b>\n\n"
                f"Connected to: <b>{self.cluster_name}</b>\n\n"
                "<b>Available Commands:</b>\n"
                "• /check - Scan running workloads vs upstream registries\n"
                "• /status - Show latest scan summary\n"
                "• /apply - Redeploy updates, commit to Git & trigger Flux\n"
                "• /help - Display this manual"
            )
            self.send_message(chat_id, msg)

        elif cmd == "/status":
            scan = self.latest_scan
            status_header = f"📊 <b>Cluster Image Status ({self.cluster_name})</b>\n"
            if self.is_scanning:
                status_header += "⏳ <i>A scan is currently running in the background...</i>\n"
            status_header += f"⏰ <b>Scheduled crawl:</b> Daily at {self.schedule_time_str} ({int(self.schedule_delay_seconds)}s paced crawl)\n"

            if not scan:
                self.send_message(
                    chat_id,
                    f"{status_header}\nℹ️ No previous scan results available yet. Use /check to start an immediate image scan.",
                )
                return

            time_str = scan.scanned_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            msg = (
                f"{status_header}\n"
                f"<i>Last scan: {time_str}</i>\n\n"
                f"• Updates available: <b>{len(scan.updates)}</b>\n"
                f"• Up to date: <b>{scan.up_to_date_count}</b>\n"
                f"• Same version (rebuilt): <b>{scan.rebuilt_count}</b>\n"
            )
            if scan.updates:
                msg += "\n<b>Pending Updates:</b>\n"
                for u in scan.updates:
                    badge = " <i>[Major]</i>" if u.is_major else ""
                    msg += f"• <code>{u.image.workload}</code>: <code>{u.running_version}</code> ➔ <code>{u.latest_version}</code>{badge}\n"
                markup = {
                    "inline_keyboard": [
                        [
                            {"text": "🚀 Apply Updates", "callback_data": "apply_updates"},
                            {"text": "❌ Dismiss", "callback_data": "dismiss"},
                        ]
                    ]
                }
                self.send_message(chat_id, msg, reply_markup=markup)
            else:
                msg += "\n✅ All cluster workloads are current."
                self.send_message(chat_id, msg)

        elif cmd == "/check":
            if self.is_scanning:
                self.send_message(chat_id, "⏳ A scan is already in progress. Please wait...")
                return

            msg_id = self.send_message(
                chat_id,
                "🔍 <b>Scanning cluster workloads and upstream registries...</b>\n<i>This usually takes 15–30 seconds.</i>",
            )

            def run_scan() -> None:
                self.is_scanning = True
                try:
                    scan = self.engine.check_updates(delay_seconds=self.manual_delay_seconds)
                    self.process_scan(scan, chat_id=chat_id, message_id=msg_id, is_scheduled=False)
                except Exception as exc:
                    logger.exception("Error during manual check")
                    err_msg = f"❌ <b>Error checking updates:</b>\n<code>{exc}</code>"
                    if msg_id:
                        self.edit_message(chat_id, msg_id, err_msg)
                    else:
                        self.send_message(chat_id, err_msg)
                finally:
                    self.is_scanning = False

            threading.Thread(target=run_scan, daemon=True).start()

        elif cmd == "/apply":
            self.execute_apply(chat_id=chat_id)

    def process_scan(
        self,
        scan: ScanResult,
        chat_id: int | None = None,
        message_id: int | None = None,
        is_scheduled: bool = False,
    ) -> None:
        """Processes scan results: auto-applies minor/patch updates and prompts only for major updates."""
        self.latest_scan = scan
        finish_time = datetime.now().strftime("%H:%M %Z")

        # Partition updates into minor/patch (auto-applicable) vs major version changes
        auto_updates = [u for u in scan.updates if not u.is_major]
        major_updates = [u for u in scan.updates if u.is_major]

        auto_applied_result: ApplyResult | None = None
        deferred_plex_note: str = ""

        if auto_updates:
            plex_auto = [u for u in auto_updates if u.is_plex]
            non_plex_auto = [u for u in auto_updates if not u.is_plex]
            updates_to_run = list(non_plex_auto)

            if plex_auto:
                is_streaming, stream_reason = self.engine.is_plex_streaming()
                if is_streaming:
                    logger.info(f"Auto-apply: Plex has active streaming ({stream_reason}), deferring Plex update.")
                    deferred_plex_note = (
                        f"⏸️ <b>Plex update deferred:</b>\n"
                        f"Active stream in progress (<i>{stream_reason}</i>). "
                        "Plex will update automatically on the next scan once playback finishes."
                    )
                else:
                    updates_to_run.extend(plex_auto)

            if updates_to_run:
                auto_applied_result = self.engine.apply_updates(updates_to_run)
                if auto_applied_result.success:
                    applied_keys = {(u.image.file, u.image.line_number) for u in updates_to_run}
                    scan.updates = [u for u in scan.updates if (u.image.file, u.image.line_number) not in applied_keys]
                else:
                    logger.error(f"Failed to auto-apply updates: {auto_applied_result.error_message}")

        # 1. Send Auto-Applied Summary if updates were applied or Plex was deferred
        if auto_applied_result and auto_applied_result.success and auto_applied_result.updated_workloads:
            summary = (
                f"⚡ <b>Auto-Applied Image Updates ({self.cluster_name})</b>\n\n"
                f"The following updates fall within the current version line and were applied automatically:\n\n"
            )
            for w in auto_applied_result.updated_workloads:
                summary += f"• <code>{w}</code>\n"
            summary += f"\n<b>Commit:</b> <code>{auto_applied_result.commit_sha}</code>\n"
            summary += "🔄 <b>Flux reconciliation triggered.</b> Pods will roll out automatically."
            if deferred_plex_note:
                summary += f"\n\n{deferred_plex_note}"

            if chat_id:
                if message_id and not major_updates:
                    self.edit_message(chat_id, message_id, summary)
                    message_id = None
                else:
                    self.send_message(chat_id, summary)
            else:
                self.broadcast(summary)

        elif deferred_plex_note and not (auto_applied_result and auto_applied_result.updated_workloads):
            msg = f"🔔 <b>Image Update Notification ({self.cluster_name})</b>\n\n{deferred_plex_note}"
            if chat_id:
                if message_id and not major_updates:
                    self.edit_message(chat_id, message_id, msg)
                    message_id = None
                else:
                    self.send_message(chat_id, msg)
            else:
                self.broadcast(msg)

        # 2. Prompt for Major Version Updates if any
        if major_updates:
            prompt = (
                f"🔔 <b>Major Version Updates Available ({self.cluster_name})</b>\n"
                f"<i>Manual approval required for major version upgrades:</i>\n\n"
            )
            for u in major_updates:
                prompt += (
                    f"• <b>{u.image.workload}</b>\n"
                    f"   <code>{u.running_version}</code> ➔ <b><code>{u.latest_version}</code></b>\n\n"
                )
            if scan.errors:
                prompt += f"⚠️ <i>{len(scan.errors)} notices/errors encountered during scan.</i>\n"

            markup = {
                "inline_keyboard": [
                    [
                        {"text": "🚀 Apply Updates", "callback_data": "apply_updates"},
                        {"text": "❌ Dismiss", "callback_data": "dismiss"},
                    ]
                ]
            }
            if chat_id:
                if message_id:
                    self.edit_message(chat_id, message_id, prompt, reply_markup=markup)
                else:
                    self.send_message(chat_id, prompt, reply_markup=markup)
            else:
                self.broadcast(prompt, reply_markup=markup)

        # 3. All Up to Date (no auto updates, no major updates, no deferred Plex)
        elif not (auto_applied_result and auto_applied_result.updated_workloads) and not deferred_plex_note:
            if chat_id:
                body = (
                    "✅ <b>All cluster workloads are up to date!</b>\n"
                    f"Inspected {scan.up_to_date_count + scan.rebuilt_count} workloads. No new versions found."
                )
                if message_id:
                    self.edit_message(chat_id, message_id, body)
                else:
                    self.send_message(chat_id, body)
            elif is_scheduled and self.notify_on_no_updates:
                self.broadcast(
                    f"🔔 <b>Daily Image Check ({self.cluster_name}):</b> "
                    f"All running workloads are current (checked at {finish_time})."
                )

    def execute_apply(self, chat_id: int, message_id: int | None = None) -> None:
        if self.is_applying:
            msg = "⏳ An update apply is already running. Please wait..."
            if message_id:
                self.edit_message(chat_id, message_id, msg)
            else:
                self.send_message(chat_id, msg)
            return

        status_msg = (
            "⏳ <b>Applying updates...</b>\n"
            "Checking Plex stream status, modifying Git manifests, committing to <code>origin/main</code>, and notifying Flux..."
        )
        if message_id:
            self.edit_message(chat_id, message_id, status_msg)
        else:
            message_id = self.send_message(chat_id, status_msg)

        def run_apply() -> None:
            self.is_applying = True
            try:
                updates = self.latest_scan.updates if self.latest_scan else None
                result: ApplyResult = self.engine.apply_updates(updates)
                if result.success:
                    if not result.updated_workloads and not result.deferred_workloads:
                        text = "ℹ️ <b>No updates needed to be applied.</b>"
                    else:
                        text = ""
                        if result.updated_workloads:
                            workloads_list = "\n".join(f"• <code>{w}</code>" for w in result.updated_workloads)
                            text += (
                                "🎉 <b>Updates successfully applied!</b>\n\n"
                                f"<b>Commit:</b> <code>{result.commit_sha}</code>\n"
                                f"<b>Updated Workloads:</b>\n{workloads_list}\n\n"
                                "🔄 <b>Flux reconciliation triggered.</b> Pods will roll out automatically.\n"
                            )
                        if result.deferred_workloads:
                            deferred_list = "\n".join(f"• <code>{w}</code>" for w in result.deferred_workloads)
                            reasons = "\n".join(f"<i>{v}</i>" for v in result.deferred_reasons.values())
                            text += (
                                f"\n⏸️ <b>Plex update postponed:</b>\n{deferred_list}\n{reasons}\n"
                                "Cannot update Plex while active streaming is in progress."
                            )

                    if self.latest_scan:
                        if result.deferred_workloads:
                            # Keep only deferred workloads in latest_scan
                            self.latest_scan.updates = [
                                u for u in self.latest_scan.updates
                                if u.is_plex or any(k in u.image.workload for k in result.deferred_reasons)
                            ]
                        else:
                            self.latest_scan.updates.clear()
                else:
                    text = f"❌ <b>Failed to apply updates:</b>\n<code>{result.error_message}</code>"

                if message_id:
                    self.edit_message(chat_id, message_id, text)
                else:
                    self.send_message(chat_id, text)
            except Exception as exc:
                logger.exception("Error executing apply")
                err_text = f"❌ <b>Exception applying updates:</b>\n<code>{exc}</code>"
                if message_id:
                    self.edit_message(chat_id, message_id, err_text)
                else:
                    self.send_message(chat_id, err_text)
            finally:
                self.is_applying = False

        threading.Thread(target=run_apply, daemon=True).start()

    def handle_callback_query(self, query: dict[str, Any]) -> None:
        query_id = query.get("id", "")
        from_user = query.get("from", {})
        user_id = from_user.get("id")
        msg = query.get("message", {})
        chat_id = msg.get("chat", {}).get("id")
        message_id = msg.get("message_id")
        data = query.get("data", "")

        if not self.is_authorized(user_id, chat_id):
            self.answer_callback(query_id, "Unauthorized")
            return

        if data == "apply_updates":
            self.answer_callback(query_id, "Starting rollout...")
            self.execute_apply(chat_id=chat_id, message_id=message_id)

        elif data == "dismiss":
            self.answer_callback(query_id, "Dismissed")
            self.edit_message(
                chat_id,
                message_id,
                "❌ <b>Image update prompt dismissed.</b> You can run /check again anytime.",
            )

    def poll_updates(self) -> None:
        url = f"{self.base_url}/getUpdates"
        offset = self.last_update_id + 1 if self.last_update_id else 0
        try:
            payload = {"offset": offset, "timeout": 25}
            res = self._request("getUpdates", payload, timeout=35)
            if not res.get("ok"):
                return
            for item in res.get("result", []):
                self.last_update_id = max(self.last_update_id, item["update_id"])
                if "message" in item:
                    msg = item["message"]
                    user_id = msg.get("from", {}).get("id")
                    chat_id = msg.get("chat", {}).get("id")
                    text = msg.get("text", "")
                    if self.is_authorized(user_id, chat_id):
                        self.handle_command(chat_id, user_id, text)
                elif "callback_query" in item:
                    self.handle_callback_query(item["callback_query"])
        except (HTTPError, URLError, TimeoutError) as exc:
            logger.debug(f"Polling timeout or network hiccup: {exc}")
        except Exception as exc:
            logger.error(f"Unexpected error in polling: {exc}")

    def run_scheduler(self) -> None:
        """Background thread for scheduled daily checks."""
        logger.info(f"Scheduler started. Daily target time: {self.schedule_time_str}")
        last_run_day: int | None = None

        while not self._stop_event.is_set():
            now = datetime.now()
            target_hour, target_minute = (int(x) for x in self.schedule_time_str.split(":"))

            if now.hour == target_hour and now.minute == target_minute and now.day != last_run_day:
                last_run_day = now.day
                logger.info(f"Executing scheduled paced image update scan (delay={self.schedule_delay_seconds}s)...")
                self.is_scanning = True
                try:
                    scan = self.engine.check_updates(delay_seconds=self.schedule_delay_seconds)
                    self.process_scan(scan, is_scheduled=True)
                except Exception as exc:
                    logger.exception("Error in scheduled check")
                finally:
                    self.is_scanning = False

            time.sleep(25)

    def start(self) -> None:
        logger.info(f"Starting Telegram Bot listener & scheduler for '{self.cluster_name}'...")
        sched_thread = threading.Thread(target=self.run_scheduler, daemon=True)
        sched_thread.start()

        self.broadcast(f"🚀 <b>GitOps Updater Bot ({self.cluster_name}) is online.</b> Type /help for commands.")

        while not self._stop_event.is_set():
            self.poll_updates()
            time.sleep(1)

    def stop(self) -> None:
        self._stop_event.set()
