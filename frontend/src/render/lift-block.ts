/**
 * Lift a single rendered block out of a one-block envelope render.
 *
 * WHY THIS IS ITS OWN FUNCTION. The renderEnvelope cutover routes blocks that
 * renderEnvelope cannot draw itself through a delegation: render a one-block
 * envelope with the existing renderer, then take the elements out. The first
 * version took `.message-bubble`'s children — and lost every block that does
 * not append to the bubble.
 *
 * `action_chips` is exactly that: it pushes into `pendingActionChips`, which
 * is drained onto the `.message` WRAPPER as a SIBLING of the bubble. So the
 * chip was rendered correctly and then thrown away. Ananth, on a live card:
 * "THE APPEALS AGENT LINK IS NOT RENDERED".
 *
 * Extracted here so the rule — take everything under `.message`, unwrapping
 * the bubble — can be tested. Buried inline in the handler it could not be.
 */
export function liftBlockElements(root: Element | null): HTMLElement | null {
  if (!root) return null;
  const msgEl = root.querySelector(".message") ?? root;
  const holder = document.createElement("div");
  holder.className = "envelope-extra-block";
  for (const child of Array.from(msgEl.children)) {
    if (child.classList.contains("message-bubble")) {
      // The bubble is a container, not content — its children are the block's.
      Array.from(child.children).forEach((c) => holder.appendChild(c));
    } else {
      // A sibling of the bubble IS content: this is where action_chips land.
      holder.appendChild(child);
    }
  }
  return holder.children.length > 0 ? holder : null;
}
