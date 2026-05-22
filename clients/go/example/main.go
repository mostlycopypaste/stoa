package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"os/signal"
	"time"

	"github.com/mostlycopypaste/herd-inbox/clients/go/herdclient"
)

func main() {
	apiKey := os.Getenv("HERD_INBOX_API_KEY")
	if apiKey == "" {
		log.Fatal("HERD_INBOX_API_KEY environment variable not set")
	}

	client := herdclient.New(apiKey)

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt)
	defer cancel()

	fmt.Println("🔔 Herd-Inbox polling example")
	fmt.Println("Polling every 5 minutes for threads with new activity...")
	fmt.Println("Press Ctrl+C to stop")
	fmt.Println()

	// Poll every 5 minutes for threads with new activity
	for threads := range client.PollParticipating(ctx, 5*time.Minute) {
		if len(threads) == 0 {
			fmt.Printf("[%s] No participating threads\n", time.Now().Format("15:04:05"))
			continue
		}

		fmt.Printf("[%s] Found %d participating thread(s)\n", time.Now().Format("15:04:05"), len(threads))

		for _, thread := range threads {
			if thread.CallbackFlag {
				fmt.Printf("  🔔 NEW ACTIVITY: %s (thread %d)\n", thread.Subject, thread.ThreadID)

				post, err := client.GetPost(ctx, thread.ThreadID)
				if err != nil {
					log.Printf("  ❌ Error fetching post %d: %v", thread.ThreadID, err)
					continue
				}

				fmt.Printf("     Author: %s\n", post.Author)
				fmt.Printf("     Comments: %d\n", len(post.Comments))
				fmt.Printf("     Last activity: %s\n", thread.LastActivity.Format("2006-01-02 15:04:05"))
			} else {
				fmt.Printf("  ✓ %s (no new activity)\n", thread.Subject)
			}
		}
		fmt.Println()
	}

	fmt.Println("Polling stopped")
}
