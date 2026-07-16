"""Central authorization policy for community post metadata and PDF reads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from community_auth import (
    AuthenticationRequired,
    AuthorizationDenied,
    RequestPrincipal,
    ResourceNotFound,
)
from community_store import Moderation, PostStatus, Visibility


@dataclass(frozen=True, slots=True)
class CommunityAuthorizationPolicy:
    """Explicit role policy; moderators do not inherit private-owner access."""

    admin_can_read_private: bool = True
    admin_can_manage_owner_posts: bool = True
    moderator_can_read_private: bool = False


DEFAULT_POLICY = CommunityAuthorizationPolicy()


def is_owner(principal: RequestPrincipal, post: dict[str, Any]) -> bool:
    return principal.authenticated and principal.user_id == str(post.get("user_id") or "")


def can_read_post(
    principal: RequestPrincipal,
    post: dict[str, Any],
    *,
    policy: CommunityAuthorizationPolicy = DEFAULT_POLICY,
) -> bool:
    """Apply one read policy before any storage provider is constructed or called."""
    if post.get("status") != PostStatus.PUBLISHED:
        return False
    visibility = post.get("visibility")
    moderation = post.get("moderation_status")
    if visibility == Visibility.PUBLIC:
        return moderation == Moderation.APPROVED
    if moderation not in {Moderation.APPROVED, Moderation.PENDING}:
        return False
    if visibility not in {Visibility.PRIVATE, Visibility.UNLISTED}:
        return False
    if is_owner(principal, post):
        return True
    if principal.authenticated and principal.has_role("admin"):
        return policy.admin_can_read_private
    if principal.authenticated and principal.has_role("moderator"):
        return policy.moderator_can_read_private
    return False


def authorize_read_post(
    principal: RequestPrincipal,
    post: dict[str, Any],
    *,
    policy: CommunityAuthorizationPolicy = DEFAULT_POLICY,
) -> None:
    if can_read_post(principal, post, policy=policy):
        return
    if not principal.authenticated and post.get("visibility") in {
        Visibility.PRIVATE,
        Visibility.UNLISTED,
    }:
        raise AuthenticationRequired("authentication_required")
    raise ResourceNotFound("post_not_found")


def can_manage_post(
    principal: RequestPrincipal,
    post: dict[str, Any],
    *,
    policy: CommunityAuthorizationPolicy = DEFAULT_POLICY,
) -> bool:
    if is_owner(principal, post):
        return True
    return bool(
        principal.authenticated
        and principal.has_role("admin")
        and policy.admin_can_manage_owner_posts
    )


def authorize_manage_post(
    principal: RequestPrincipal,
    post: dict[str, Any],
    *,
    policy: CommunityAuthorizationPolicy = DEFAULT_POLICY,
) -> None:
    if not principal.authenticated:
        raise AuthenticationRequired("authentication_required")
    if can_manage_post(principal, post, policy=policy):
        return
    raise ResourceNotFound("post_not_found")


def authorize_delete_file(
    principal: RequestPrincipal,
    post: dict[str, Any],
    *,
    policy: CommunityAuthorizationPolicy = DEFAULT_POLICY,
) -> None:
    """Owner/admin policy for destructive storage actions."""
    authorize_manage_post(principal, post, policy=policy)


def authorize_moderation(principal: RequestPrincipal) -> None:
    """Moderation accepts only server-issued admin or moderator roles."""
    if not principal.authenticated:
        raise AuthenticationRequired("authentication_required")
    if not (principal.has_role("admin") or principal.has_role("moderator")):
        raise AuthorizationDenied("moderator_required")


def authorize_admin_operation(principal: RequestPrincipal) -> None:
    """Reconciliation and administrative reports remain admin-only."""
    if not principal.authenticated:
        raise AuthenticationRequired("authentication_required")
    if not principal.has_role("admin"):
        raise AuthorizationDenied("admin_required")
