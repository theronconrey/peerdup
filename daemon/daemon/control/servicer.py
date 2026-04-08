"""
ControlService gRPC servicer.

Handles CLI requests over a Unix domain socket.
Delegates all business logic to SyncCoordinator.
No TLS — Unix socket permissions provide access control.
"""

from __future__ import annotations

import asyncio
import logging

import grpc

log = logging.getLogger(__name__)


class ControlServicer:

    def __init__(self, coordinator):
        self._coord = coordinator

        import control_pb2      # type: ignore
        import control_pb2_grpc  # type: ignore
        self._pb     = control_pb2
        self._pb_grpc = control_pb2_grpc

    # ── Share management ──────────────────────────────────────────────────────

    def CreateShare(self, request, context):
        pb = self._pb
        try:
            loop   = asyncio.new_event_loop()
            result = loop.run_until_complete(
                self._coord.create_share(
                    request.name,
                    request.local_path,
                    request.permission or "rw",
                    import_key_hex=request.import_key_hex or "",
                )
            )
            loop.close()
            return pb.CreateShareResponse(
                share    = self._dict_to_proto(result),
                share_id = result["share_id"],
            )
        except Exception as e:
            log.exception("CreateShare failed")
            context.abort(grpc.StatusCode.INTERNAL, str(e))

    def AddShare(self, request, context):
        pb = self._pb
        try:
            loop   = asyncio.new_event_loop()
            status = loop.run_until_complete(
                self._coord.add_share(
                    request.share_id,
                    request.local_path,
                    request.permission or "rw",
                )
            )
            loop.close()
            return pb.AddShareResponse(share=self._dict_to_proto(status))
        except Exception as e:
            log.exception("AddShare failed")
            context.abort(grpc.StatusCode.INTERNAL, str(e))

    def RemoveShare(self, request, context):
        pb = self._pb
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(
                self._coord.remove_share(request.share_id, request.delete_files)
            )
            loop.close()
            return pb.RemoveShareResponse()
        except KeyError as e:
            context.abort(grpc.StatusCode.NOT_FOUND, str(e))
        except Exception as e:
            log.exception("RemoveShare failed")
            context.abort(grpc.StatusCode.INTERNAL, str(e))

    def ListShares(self, request, context):
        pb = self._pb
        shares = self._coord.list_shares()
        return pb.ListSharesResponse(
            shares=[self._dict_to_proto(s) for s in shares]
        )

    def PauseShare(self, request, context):
        pb = self._pb
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(self._coord.pause_share(request.share_id))
            loop.close()
            return pb.PauseShareResponse()
        except KeyError as e:
            context.abort(grpc.StatusCode.NOT_FOUND, str(e))

    def ResumeShare(self, request, context):
        pb = self._pb
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(self._coord.resume_share(request.share_id))
            loop.close()
            return pb.ResumeShareResponse()
        except KeyError as e:
            context.abort(grpc.StatusCode.NOT_FOUND, str(e))

    # ── Rate limits ───────────────────────────────────────────────────────────

    def SetShareRateLimit(self, request, context):
        pb = self._pb
        try:
            result = self._coord.set_share_rate_limit(
                request.share_id,
                request.upload_limit,
                request.download_limit,
            )
            return pb.SetShareRateLimitResponse(
                share_id       = result["share_id"],
                upload_limit   = result["upload_limit"],
                download_limit = result["download_limit"],
            )
        except KeyError as e:
            context.abort(grpc.StatusCode.NOT_FOUND, str(e))
        except Exception as e:
            log.exception("SetShareRateLimit failed")
            context.abort(grpc.StatusCode.INTERNAL, str(e))

    # ── Share info ────────────────────────────────────────────────────────────

    def GetShareInfo(self, request, context):
        pb = self._pb
        try:
            d = self._coord.get_share_info(request.share_id)
            return pb.GetShareInfoResponse(
                share_id     = d["share_id"],
                name         = d["name"],
                local_path   = d["local_path"],
                state        = d["state"],
                permission   = d["permission"],
                is_owner     = d["is_owner"],
                owner_id     = d["owner_id"],
                created_at   = d["created_at"],
                bytes_total  = d["bytes_total"],
                bytes_done   = d["bytes_done"],
                info_hash    = d["info_hash"],
                peers_online   = d["peers_online"],
                total_peers    = d["total_peers"],
                last_error     = d["last_error"],
                upload_limit   = d["upload_limit"],
                download_limit = d["download_limit"],
            )
        except KeyError as e:
            context.abort(grpc.StatusCode.NOT_FOUND, str(e))
        except Exception as e:
            log.exception("GetShareInfo failed")
            context.abort(grpc.StatusCode.INTERNAL, str(e))

    # ── Peer roster ───────────────────────────────────────────────────────────

    def ListSharePeers(self, request, context):
        pb = self._pb
        try:
            result = self._coord.list_share_peers(request.share_id)

            peers_proto = []
            for p in result["peers"]:
                peers_proto.append(pb.SharePeerInfo(
                    peer_id       = p["peer_id"],
                    name          = p["name"],
                    permission    = p["permission"],
                    online        = p["online"],
                    is_self       = p["is_self"],
                    is_owner      = p["is_owner"],
                    last_seen     = p["last_seen"],
                    addresses     = p["addresses"],
                    download_rate = p["download_rate"],
                    upload_rate   = p["upload_rate"],
                ))

            return pb.ListSharePeersResponse(
                share_id      = result["share_id"],
                share_name    = result["share_name"],
                peers         = peers_proto,
                total_granted = result["total_granted"],
                total_online  = result["total_online"],
            )
        except KeyError as e:
            context.abort(grpc.StatusCode.NOT_FOUND, str(e))
        except Exception as e:
            log.exception("ListSharePeers failed")
            context.abort(grpc.StatusCode.INTERNAL, str(e))

    # ── Access control ───────────────────────────────────────────────────────

    def GrantAccess(self, request, context):
        pb = self._pb
        try:
            loop   = asyncio.new_event_loop()
            result = loop.run_until_complete(
                self._coord.grant_access(
                    request.share_id,
                    request.peer_id,
                    request.permission or "rw",
                )
            )
            loop.close()
            return pb.GrantAccessResponse(
                share_id   = result["share_id"],
                peer_id    = result["peer_id"],
                permission = result["permission"],
            )
        except KeyError as e:
            context.abort(grpc.StatusCode.NOT_FOUND, str(e))
        except PermissionError as e:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, str(e))
        except Exception as e:
            log.exception("GrantAccess failed")
            context.abort(grpc.StatusCode.INTERNAL, str(e))

    def RevokeAccess(self, request, context):
        pb = self._pb
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(
                self._coord.revoke_access(request.share_id, request.peer_id)
            )
            loop.close()
            return pb.RevokeAccessResponse()
        except KeyError as e:
            context.abort(grpc.StatusCode.NOT_FOUND, str(e))
        except PermissionError as e:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, str(e))
        except Exception as e:
            log.exception("RevokeAccess failed")
            context.abort(grpc.StatusCode.INTERNAL, str(e))

    # ── Identity ──────────────────────────────────────────────────────────────

    def ShowIdentity(self, request, context):
        pb = self._pb
        return pb.IdentityResponse(
            peer_id = self._coord.peer_id,
            name    = "",  # populated from state db in coordinator
        )

    # ── Status ────────────────────────────────────────────────────────────────

    def Status(self, request, context):
        pb = self._pb
        shares = self._coord.list_shares()
        if shares:
            s = shares[0]
            return pb.StatusEvent(
                type    = pb.StatusEvent.EVENT_TYPE_SHARE_UPDATED,
                share   = self._dict_to_proto(s),
                message = f"{len(shares)} shares active",
            )
        return pb.StatusEvent(
            type    = pb.StatusEvent.EVENT_TYPE_UNSPECIFIED,
            message = "No active shares",
        )

    def WatchStatus(self, request, context):
        """Server-streaming: push StatusEvents to the CLI."""
        pb = self._pb
        import queue
        import threading

        live_q: queue.Queue = queue.Queue(maxsize=128)
        async_q: asyncio.Queue = asyncio.Queue(maxsize=128)

        self._coord.subscribe_control(async_q)

        # Bridge asyncio queue → threading queue for the sync gRPC servicer.
        loop   = asyncio.new_event_loop()
        stop_t = threading.Event()

        async def _drain():
            while not stop_t.is_set():
                try:
                    item = await asyncio.wait_for(async_q.get(), timeout=1.0)
                    live_q.put(item)
                except asyncio.TimeoutError:
                    continue

        t = threading.Thread(
            target=lambda: loop.run_until_complete(_drain()),
            daemon=True,
        )
        t.start()

        try:
            while context.is_active():
                try:
                    event_dict = live_q.get(timeout=1.0)
                    yield self._dict_event_to_proto(event_dict)
                except queue.Empty:
                    continue
        finally:
            stop_t.set()
            self._coord.unsubscribe_control(async_q)
            t.join(timeout=3)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _dict_to_proto(self, d: dict):
        pb = self._pb
        return pb.ShareStatus(
            share_id     = d.get("share_id", ""),
            name         = d.get("name", ""),
            local_path   = d.get("local_path", ""),
            state        = d.get("state", ""),
            bytes_total  = d.get("bytes_total", 0),
            bytes_done   = d.get("bytes_done", 0),
            peers_online = d.get("peers_online", 0),
            last_error   = d.get("last_error", ""),
            info_hash    = d.get("info_hash", ""),
        )

    def _dict_event_to_proto(self, d: dict):
        pb = self._pb
        type_map = {
            "share_updated":  pb.StatusEvent.EVENT_TYPE_SHARE_UPDATED,
            "peer_event":     pb.StatusEvent.EVENT_TYPE_PEER_EVENT,
            "sync_progress":  pb.StatusEvent.EVENT_TYPE_SYNC_PROGRESS,
            "error":          pb.StatusEvent.EVENT_TYPE_ERROR,
        }
        etype = type_map.get(d.get("type", ""),
                             pb.StatusEvent.EVENT_TYPE_UNSPECIFIED)
        return pb.StatusEvent(
            type    = etype,
            message = str(d),
        )
