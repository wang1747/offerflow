import { useEffect, useState } from "react";
import { getStatus, subscribe, type OfflineStatus } from "./index";

/** 订阅离线状态。组件卸载时自动退订。 */
export function useOffline(): OfflineStatus {
  const [status, setStatus] = useState<OfflineStatus>(getStatus);
  useEffect(() => {
    const unsubscribe = subscribe(setStatus);
    return () => {
      unsubscribe();
    };
  }, []);
  return status;
}
