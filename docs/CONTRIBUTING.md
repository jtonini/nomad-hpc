# Contributing to NØMAÐ

Thank you for your interest in contributing to NØMAÐ! This document provides guidelines and information for contributors.

## Code of Conduct

By participating in this project, you agree to maintain a respectful and inclusive environment. We welcome contributors of all backgrounds and experience levels.

## How to Contribute

### Reporting Issues

- **Bug reports**: Use the GitHub issue tracker with the "bug" label
- **Feature requests**: Use the "enhancement" label
- **Questions**: Use the "question" label or start a discussion

When reporting a bug, please include:
- NØMAÐ version
- Python version
- Operating system
- SLURM version (if relevant)
- Steps to reproduce
- Expected vs actual behavior
- Error messages and logs

### Contributing Code

1. **Fork** the repository
2. **Create a branch** for your feature (`git checkout -b feature/amazing-feature`)
3. **Make your changes** following our coding standards
4. **Add tests** for new functionality
5. **Run the test suite** (`pytest`)
6. **Commit** with a clear message
7. **Push** to your branch
8. **Open a Pull Request**

### Pull Request Guidelines

- Keep PRs focused on a single feature or fix
- Update documentation if needed
- Add tests for new code
- Ensure all tests pass
- Follow the existing code style
- Write a clear PR description explaining your changes

## Development Setup

```bash
# Clone your fork
git clone https://github.com/YOUR_USERNAME/nomad.git
cd nomad

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install in development mode with dev dependencies
pip install -e ".[dev]"

# Install pre-commit hooks
pre-commit install
```

## Coding Standards

### Python Style

- Follow PEP 8
- Use type hints for function signatures
- Maximum line length: 100 characters
- Use `ruff` for linting: `ruff check .`
- Use `black` for formatting: `black .`

### Documentation

- Use docstrings for all public functions and classes
- Follow Google docstring format
- Update README.md for user-facing changes
- Update docs/ for detailed documentation

### Example Function

```python
def calculate_health_score(
    metrics: dict[str, float],
    weights: dict[str, float] | None = None,
) -> float:
    """
    Calculate job health score from metrics.

    Args:
        metrics: Dictionary of metric name to value.
            Expected keys: cpu_percent, mem_gb, nfs_write_gb, etc.
        weights: Optional custom weights for each metric.
            Defaults to empirically derived weights.

    Returns:
        Health score between 0.0 (catastrophic) and 1.0 (perfect).

    Raises:
        ValueError: If required metrics are missing.

    Example:
        >>> metrics = {'cpu_percent': 85, 'nfs_write_gb': 50}
        >>> score = calculate_health_score(metrics)
        >>> print(f"Health: {score:.2f}")
        Health: 0.72
    """
    ...
```

### Commit Messages

Follow the conventional commits format:

```
type(scope): brief description

Longer description if needed.

Fixes #123
```

Types:
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation only
- `style`: Formatting, no code change
- `refactor`: Code restructuring
- `test`: Adding tests
- `chore`: Maintenance tasks

## Testing

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=nomad --cov-report=html

# Run specific test file
pytest tests/test_collectors.py

# Run specific test
pytest tests/test_collectors.py::test_disk_collector
```

### Writing Tests

- Place tests in `tests/` directory
- Mirror the source structure (e.g., `tests/test_collectors/test_disk.py`)
- Use pytest fixtures for common setup
- Mock external dependencies (SLURM commands, filesystem, etc.)

Example test:

```python
import pytest
from unittest.mock import patch, MagicMock
from nomad.collectors.disk import DiskCollector

@pytest.fixture
def disk_collector():
    """Create a DiskCollector with test configuration."""
    config = {
        'filesystems': ['/home', '/scratch'],
        'quota_enabled': False,
    }
    return DiskCollector(config)

def test_parse_df_output(disk_collector):
    """Test parsing of df command output."""
    df_output = """Filesystem     1K-blocks      Used Available Use% Mounted on
/dev/sda1      102400000  51200000  51200000  50% /home"""
    
    result = disk_collector._parse_df_output(df_output)
    
    assert len(result) == 1
    assert result[0]['path'] == '/home'
    assert result[0]['used_percent'] == 50.0

@patch('subprocess.run')
def test_collect_filesystem_usage(mock_run, disk_collector):
    """Test filesystem usage collection."""
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="...",  # df output
    )
    
    result = disk_collector.collect()
    
    assert result is not None
    mock_run.assert_called_once()
```

## Project Structure

```
nomad/
├── nomad/              # Main package
│   ├── collectors/      # Data collectors
│   ├── db/             # Database layer
│   ├── analysis/       # Analysis utilities
│   ├── alerts/         # Alert system
│   ├── prediction/     # ML prediction
│   └── viz/            # Visualization
├── tests/              # Test suite
├── docs/               # Documentation
├── scripts/            # Utility scripts
└── examples/           # Example configurations
```

## Areas for Contribution

### High Priority
- [ ] Additional SLURM metrics collection
- [ ] Prometheus export format
- [ ] Grafana dashboard templates
- [ ] More comprehensive test coverage

### Medium Priority
- [ ] Additional license server types
- [ ] Custom alert dispatch channels
- [ ] Dark mode for dashboard
- [ ] Mobile-responsive dashboard

### Future / Research
- [ ] GNN model implementation
- [ ] LSTM early warning system
- [ ] Federated learning across clusters
- [ ] Natural language alert summaries

## Getting Help

- **Documentation**: Check the `docs/` directory
- **Issues**: Search existing issues before creating new ones
- **Discussions**: Use GitHub Discussions for questions
- **Email**: [contact email]

## License

By contributing, you agree that your contributions will be licensed under the same license as the project (AGPL v3 for open source use).

## Recognition

Contributors will be recognized in:
- The project README
- Release notes
- The AUTHORS file

Thank you for contributing to NØMAÐ! 🎉
