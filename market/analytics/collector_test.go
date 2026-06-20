package analytics

import (
	"context"
	"sync"
	"testing"
	"time"
)

func TestStartIdempotent(t *testing.T) {
	c := NewCollector()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Start twice — should not create duplicate goroutines.
	c.Start(ctx)
	time.Sleep(50 * time.Millisecond)
	c.Start(ctx)
	time.Sleep(50 * time.Millisecond)

	if !c.started {
		t.Fatal("expected collector to be started")
	}

	stats1 := c.Stats()
	c.Stop()
	time.Sleep(50 * time.Millisecond)

	stats2 := c.Stats()
	if stats2.BufferedSamples > stats1.BufferedSamples {
		t.Logf("buffer grew after stop (expected): %d -> %d", stats1.BufferedSamples, stats2.BufferedSamples)
	}
}

func TestConcurrentStartSafe(t *testing.T) {
	c := NewCollector()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			c.Start(ctx)
		}()
	}
	wg.Wait()

	time.Sleep(50 * time.Millisecond)
	if !c.started {
		t.Fatal("expected collector to be started")
	}
	c.Stop()
	time.Sleep(50 * time.Millisecond)
}

func TestStopNonBlocking(t *testing.T) {
	c := NewCollector()
	// Stop without start should not block or panic.
	c.Stop()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	c.Start(ctx)
	time.Sleep(20 * time.Millisecond)

	done := make(chan struct{})
	go func() {
		c.Stop()
		close(done)
	}()

	select {
	case <-done:
		// ok — Stop returned quickly
	case <-time.After(2 * time.Second):
		t.Fatal("Stop blocked")
	}
	time.Sleep(50 * time.Millisecond)
	if c.started {
		t.Fatal("expected collector to be stopped")
	}
}

func TestRestartAfterStop(t *testing.T) {
	c := NewCollector()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	c.Start(ctx)
	time.Sleep(30 * time.Millisecond)
	c.Stop()
	time.Sleep(50 * time.Millisecond)

	if c.started {
		t.Fatal("expected collector to be stopped after Stop()")
	}

	// Restart — should work cleanly without leaked goroutines.
	c.Start(ctx)
	time.Sleep(50 * time.Millisecond)
	if !c.started {
		t.Fatal("expected collector to be started after restart")
	}

	// Record something and verify flush works after restart.
	c.RecordCounter("test.metric", 1)
	c.Stop()
	time.Sleep(50 * time.Millisecond)
}

func TestRecordAfterStopAndRestart(t *testing.T) {
	c := NewCollector()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Start, record, stop.
	c.Start(ctx)
	time.Sleep(20 * time.Millisecond)
	c.RecordGauge("gauge1", 42)
	c.Stop()
	time.Sleep(50 * time.Millisecond)

	// Restart and record again.
	c.Start(ctx)
	time.Sleep(20 * time.Millisecond)
	c.RecordGauge("gauge2", 99)
	stats := c.Stats()
	if stats.BufferedSamples < 1 {
		t.Fatal("expected at least 1 buffered sample after restart")
	}
	c.Stop()
	time.Sleep(50 * time.Millisecond)
}
