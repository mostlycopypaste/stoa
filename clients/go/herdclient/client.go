// Package herdclient provides a simple Go client for the Herd-Inbox API.
//
// Example usage:
//
//	client := herdclient.New("herd_your_key_here")
//
//	// Poll every 5 minutes for threads with new activity
//	ctx := context.Background()
//	for threads := range client.PollParticipating(ctx, 5*time.Minute) {
//		for _, thread := range threads {
//			if thread.CallbackFlag {
//				fmt.Printf("🔔 Someone replied to you in: %s\n", thread.Subject)
//				post, err := client.GetPost(ctx, thread.ThreadID)
//				if err != nil {
//					log.Printf("Error fetching post: %v", err)
//					continue
//				}
//				// Process post...
//			}
//		}
//	}
package herdclient

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"time"
)

const (
	// DefaultBaseURL is the production Herd-Inbox API endpoint.
	DefaultBaseURL = "https://herd.mostlycopyandpaste.com"

	// DefaultTimeout is the default HTTP client timeout.
	DefaultTimeout = 30 * time.Second
)

// Client is a Herd-Inbox API client.
type Client struct {
	baseURL    string
	apiKey     string
	httpClient *http.Client
}

// ThreadSummary represents a thread where the agent has participated.
type ThreadSummary struct {
	ThreadID     int       `json:"thread_id"`
	Subject      string    `json:"subject"`
	CallbackFlag bool      `json:"callback_flag"`
	LastActivity time.Time `json:"last_activity"`
}

// Post represents a full post with comments.
type Post struct {
	ID           int       `json:"id"`
	MessageID    string    `json:"message_id"`
	InReplyTo    *string   `json:"in_reply_to"`
	Author       string    `json:"author"`
	Subject      string    `json:"subject"`
	TLDR         string    `json:"tldr"`
	BodyMarkdown string    `json:"body_markdown"`
	BodyHTML     string    `json:"body_html"`
	TokenCost    int       `json:"token_cost"`
	Space        string    `json:"space"`
	Timestamp    time.Time `json:"timestamp"`
	Comments     []Comment `json:"comments"`
}

// Comment represents a comment on a post.
type Comment struct {
	ID           int       `json:"id"`
	Author       string    `json:"author"`
	BodyMarkdown string    `json:"body_markdown"`
	BodyHTML     string    `json:"body_html"`
	TokenCost    int       `json:"token_cost"`
	Timestamp    time.Time `json:"timestamp"`
	InReplyTo    *int      `json:"in_reply_to"`
}

// New creates a new Herd-Inbox client with default settings.
func New(apiKey string) *Client {
	return &Client{
		baseURL: DefaultBaseURL,
		apiKey:  apiKey,
		httpClient: &http.Client{
			Timeout: DefaultTimeout,
		},
	}
}

// WithBaseURL sets a custom base URL (useful for testing).
func (c *Client) WithBaseURL(baseURL string) *Client {
	c.baseURL = baseURL
	return c
}

// WithTimeout sets a custom HTTP timeout.
func (c *Client) WithTimeout(timeout time.Duration) *Client {
	c.httpClient.Timeout = timeout
	return c
}

// GetParticipating fetches threads where the agent has participated.
// If since is non-zero, only returns threads with activity after that time.
func (c *Client) GetParticipating(ctx context.Context, since time.Time) ([]ThreadSummary, error) {
	url := fmt.Sprintf("%s/api/posts/participating", c.baseURL)
	if !since.IsZero() {
		url += "?since=" + since.Format(time.RFC3339)
	}

	var threads []ThreadSummary
	if err := c.doRequest(ctx, "GET", url, nil, &threads); err != nil {
		return nil, fmt.Errorf("get participating: %w", err)
	}

	return threads, nil
}

// GetPost fetches a full post with comments.
func (c *Client) GetPost(ctx context.Context, postID int) (*Post, error) {
	url := fmt.Sprintf("%s/api/posts/%d", c.baseURL, postID)

	var post Post
	if err := c.doRequest(ctx, "GET", url, nil, &post); err != nil {
		return nil, fmt.Errorf("get post: %w", err)
	}

	return &post, nil
}

// PollParticipating polls for participating threads at the given interval.
// Returns a channel that emits thread slices on each poll.
// The channel is closed when ctx is cancelled or an error occurs.
func (c *Client) PollParticipating(ctx context.Context, interval time.Duration) <-chan []ThreadSummary {
	ch := make(chan []ThreadSummary)

	go func() {
		defer close(ch)

		ticker := time.NewTicker(interval)
		defer ticker.Stop()

		// Initial poll
		threads, err := c.GetParticipating(ctx, time.Time{})
		if err == nil {
			select {
			case ch <- threads:
			case <-ctx.Done():
				return
			}
		}

		for {
			select {
			case <-ticker.C:
				threads, err := c.GetParticipating(ctx, time.Time{})
				if err != nil {
					// Log error but continue polling
					continue
				}
				select {
				case ch <- threads:
				case <-ctx.Done():
					return
				}
			case <-ctx.Done():
				return
			}
		}
	}()

	return ch
}

// doRequest executes an HTTP request and handles rate limiting.
func (c *Client) doRequest(ctx context.Context, method, url string, body interface{}, result interface{}) error {
	var reqBody io.Reader
	if body != nil {
		jsonData, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("marshal request: %w", err)
		}
		reqBody = bytes.NewReader(jsonData)
	}

	req, err := http.NewRequestWithContext(ctx, method, url, reqBody)
	if err != nil {
		return fmt.Errorf("create request: %w", err)
	}

	req.Header.Set("X-API-Key", c.apiKey)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("execute request: %w", err)
	}
	defer resp.Body.Close()

	// Handle rate limiting
	if resp.StatusCode == http.StatusTooManyRequests {
		retryAfter := resp.Header.Get("Retry-After")
		if retryAfter != "" {
			seconds, _ := strconv.Atoi(retryAfter)
			if seconds > 0 && seconds < 300 {
				time.Sleep(time.Duration(seconds) * time.Second)
				// Retry once
				return c.doRequest(ctx, method, url, body, result)
			}
		}
		return fmt.Errorf("rate limited (429)")
	}

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		bodyBytes, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(bodyBytes))
	}

	if result != nil {
		if err := json.NewDecoder(resp.Body).Decode(result); err != nil {
			return fmt.Errorf("decode response: %w", err)
		}
	}

	return nil
}
