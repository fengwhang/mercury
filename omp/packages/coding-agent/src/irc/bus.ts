/** Compatibility exports for older SDK integrations. */
export * from "../mirc/bus";
export { MircBus as IrcBus, MircAwaitTargetStopped as IrcAwaitTargetStopped } from "../mirc/bus";
export type { MircMessage as IrcMessage, MircDeliveryReceipt as IrcDeliveryReceipt } from "../mirc/bus";
